"""Regression tests for the B7 red-team #3 findings (final fix wave, orchestrator core).

Each test targets one finding from the third integration-layer red-team pass;
see `factory-controller/README.md` for the fix each one guards.
"""
from factory.controller.approvals import IneligibleApprover
from factory.demo import fixtures
from factory.demo.harness import new_context
from factory.demo.scenarios import _architect, _implementer, _intake, _reviewer, task
from factory.github.webhooks import parse_event
from factory.wiring import build_factory


# ── item 2 (H1): durable queues -- two Factory instances share one store ────────
def test_two_factory_instances_share_one_store_for_webhooks_and_decisions():
    """Simulates a `serve` process and a `worker` process: one instance only
    enqueues/records, a SEPARATE instance (sharing the same store) claims and
    acts. Neither the webhook inbox nor the approval-decision handling may be an
    in-process queue, or this could never work (red team #3 H1)."""
    ctx = new_context()
    try:
        serve = ctx.factory
        worker = build_factory(ctx.settings, clock=ctx.clock, adapters=ctx.adapters)

        runtime = ctx.adapters["runtime"]
        runtime.set_script("intake", _intake("patch", "AC3"))
        runtime.set_script("implementer", _implementer({"app/greet.py": fixtures.GREET_APP,
                                                        "tests/test_greet.py": fixtures.GREET_TEST_PASS}))
        runtime.set_script("reviewer", _reviewer("pass"))

        payload = {"action": "labeled",
                  "issue": {"number": 101, "title": "A patch labeled on 'serve', processed by 'worker'",
                            "body": "please fix", "user": {"login": "@dev"}},
                  "label": {"name": "factory:patch"},
                  "repository": {"full_name": ctx.repo}}
        assert parse_event("issues", payload) is not None

        # "serve" only enqueues -- durably, in the shared store.
        assert serve.enqueue_webhook("delivery-1", "issues", payload) is True
        assert serve.enqueue_webhook("delivery-1", "issues", payload) is False, "a duplicate delivery must be a no-op"
        assert serve.store.find_mission_by_work_item(f"{ctx.repo}#101") is None, \
            "enqueue_webhook must never itself run the mission"

        # "worker" claims and actually processes it.
        worker.dispatch_webhooks()
        mission = serve.store.find_mission_by_work_item(f"{ctx.repo}#101")
        assert mission is not None and mission.state == "ACTIVE", "a covered patch is admitted straight through"

        # Drive it to AWAITING_HM using either instance (both see the same store).
        serve.run_ready_tasks(mission.mission_id)
        mission = serve.store.get_mission(mission.mission_id)
        assert mission.state == "AWAITING_HM"
        request = next(r for r in serve.store.approvals.list_open(mission.mission_id) if r.gate == "HM")

        # "serve" only records the decision...
        approver = ctx.product.approvers_for("HM")[0]
        roles = ctx.product.roles_for(approver)
        serve.decide(request.request_id, approver, roles, "approve", request.content_hash)
        assert serve.store.get_mission(mission.mission_id).state == "AWAITING_HM", \
            "decide() alone must not advance the mission, on either instance"

        # ...and "worker" (a DIFFERENT Factory instance) is what actually merges it.
        worker.process_approvals()
        assert serve.store.get_mission(mission.mission_id).state == "MERGED"
    finally:
        ctx.cleanup()


# ── item 3 (H2): approvers are per gate AND per product, never a global union ────
def test_non_listed_owner_cannot_approve_h2():
    ctx = new_context()
    try:
        runtime = ctx.adapters["runtime"]
        runtime.set_script("intake", _intake("feature", "AC4"))
        runtime.set_script("architect", _architect(tasks=[task("T-1", "Add a greet() helper with a test.")]))

        item = ctx.add_work_item(title="A feature awaiting H1", body="...", labels=("factory:feature",))
        mission = ctx.start(item)
        request = ctx.open_request(mission.mission_id, "H1")

        # @backup is a real owner of this product (the `backup` role) but is not in
        # H1's `approvers: {h1: [...]}` list -- must be rejected, never silently
        # accepted just because they're SOME owner of the product.
        try:
            ctx.factory.decide(request.request_id, "@backup", ("backup",), "approve", request.content_hash)
            raised = False
        except IneligibleApprover:
            raised = True
        assert raised, "an owner not listed for this gate must be rejected"
        assert ctx.mission(mission.mission_id).state == "AWAITING_H1"

        # The actually-listed approver works.
        ctx.decide(mission.mission_id, "H1", "approve")
        assert ctx.mission(mission.mission_id).state == "ACTIVE"
    finally:
        ctx.cleanup()


def test_eligible_approvers_never_bleed_across_products(tmp_path):
    """Two separate product cells, each with a DIFFERENT tech_lead. An approver
    eligible for product A's HM must not be accepted for product B's HM, even
    though both requests are open at the same time in the same store."""
    import shutil

    from factory.demo.harness import _init_mirror
    from factory.pipeline.products import ProductRegistry
    from factory.policy.loader import default_kit_dir
    import yaml

    kit_dir = default_kit_dir()
    products_dir = tmp_path / "products"
    for name, tech_lead in (("org__product-a", "@tl-a"), ("org__product-b", "@tl-b")):
        product_dir = products_dir / name
        shutil.copytree(kit_dir / "templates" / "backend-service", product_dir)
        factory_yaml = product_dir / "factory.yaml"
        doc = yaml.safe_load(factory_yaml.read_text())
        doc["product"] = name.replace("org__", "")
        doc["owners"]["tech_lead"] = tech_lead
        doc["approvers"]["hm"] = "codeowners"
        doc["verification"]["required"] = list({*doc["verification"]["required"],
                                                 "review_agent", "holdout_blackbox"})
        factory_yaml.write_text(yaml.safe_dump(doc))
        _init_mirror(product_dir)

    registry = ProductRegistry(products_dir, kit_dir)
    product_a = registry.get("product-a")
    product_b = registry.get("product-b")

    eligible_a = product_a.eligible_for("hm")
    eligible_b = product_b.eligible_for("hm")
    assert set(eligible_a) == {"@tl-a"}
    assert set(eligible_b) == {"@tl-b"}
    assert "@tl-b" not in eligible_a
    assert "@tl-a" not in eligible_b
