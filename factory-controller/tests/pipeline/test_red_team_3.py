"""Regression tests for the B7 red-team #3 findings (final fix wave, orchestrator core).

Each test targets one finding from the third integration-layer red-team pass;
see `factory-controller/README.md` for the fix each one guards.
"""
from factory.controller.approvals import IneligibleApprover
from factory.demo import fixtures
from factory.demo.harness import new_context
from factory.demo.scenarios import (_architect, _implementer, _intake, _reviewer,
                                    advance_h2_to_observing, advance_post_merge_ok, advance_to_merged,
                                    run_feature_to_merged, task)
from factory.github.webhooks import parse_event
from factory.wiring import build_factory


def _run_standing_patch_to_observing(ctx, *, title: str, files: dict):
    """A covered (standing-mandate) patch, all the way through to a standing (no
    human H2) production deploy -- needs L5 autonomy (autonomy-levels.yaml's only
    "h2: standing" relaxation for AC1-3), which no L3 demo scenario reaches."""
    # FakeGitHub's "main" head only ever moves in-memory (a merge sha with no
    # matching commit in the real git mirror LocalSandbox checks out from); reset
    # it to the ORIGINAL real commit before every mission in a multi-mission test
    # so the next sandbox.create() has something real to check out.
    ctx.adapters["github"].set_head(ctx.repo, "main", ctx.base_commit)
    runtime = ctx.adapters["runtime"]
    runtime.set_script("intake", _intake("patch", "AC3"))
    runtime.set_script("implementer", _implementer(files))
    runtime.set_script("reviewer", _reviewer("pass"))
    item = ctx.add_work_item(title=title, body="...", labels=("factory:patch",))
    mission = ctx.start(item)
    assert mission.state == "ACTIVE", mission.state  # standing mandate: no H1
    ctx.factory.run_ready_tasks(mission.mission_id)
    advance_to_merged(ctx, mission.mission_id)
    advance_post_merge_ok(ctx, mission.mission_id)
    return mission.mission_id


# ── item 1 (C1/H3): deploy fencing tokens are real, never a constant ────────────
def test_standing_h2_second_release_actually_deployed():
    """Two consecutive standing (auto-H2) releases to production. A constant
    fencing token would make the SECOND one collide with the first's already-used
    token at the target's own fencing check (red team #3 C1)."""
    ctx = new_context(risk_profile="experimental", autonomy_level="L5")
    try:
        first_id = _run_standing_patch_to_observing(ctx, title="The first standing release for the test",
                                                    files={"app/first.py": "VALUE = 1\n"})
        assert ctx.mission(first_id).state == "OBSERVING", ctx.mission(first_id).state
        ctx.clock.advance(hours=25)
        ctx.factory.advance_observation(ctx.mission(first_id))
        assert ctx.mission(first_id).state == "DELIVERED"
        first_artifact = ctx.adapters["deploy"].receipts[-1].artifact

        second_id = _run_standing_patch_to_observing(ctx, title="The second standing release for the test",
                                                     files={"app/second.py": "VALUE = 2\n"})
        assert ctx.mission(second_id).state == "OBSERVING", ctx.mission(second_id).state
        ctx.clock.advance(hours=25)
        ctx.factory.advance_observation(ctx.mission(second_id))
        assert ctx.mission(second_id).state == "DELIVERED", \
            "a constant fencing token would make this deploy collide with the first release's"
        second_artifact = ctx.adapters["deploy"].receipts[-1].artifact
        assert second_artifact != first_artifact
    finally:
        ctx.cleanup()


def test_second_release_can_reach_staging():
    """Same concern, one layer earlier: two releases' STAGING deploys must both
    succeed (a constant staging fencing token would break the second one too)."""
    ctx = new_context(risk_profile="experimental", autonomy_level="L5")
    try:
        for i, contents in enumerate(({"app/a.py": "A = 1\n"}, {"app/b.py": "B = 2\n"}), start=1):
            mission_id = _run_standing_patch_to_observing(ctx, title=f"Standing release number {i} for the test", files=contents)
            assert ctx.mission(mission_id).state == "OBSERVING", (i, ctx.mission(mission_id).state)
        staging_receipts = [r for r in ctx.adapters["deploy"].receipts if r.environment == "staging"]
        assert len(staging_receipts) == 2
    finally:
        ctx.cleanup()


def test_deploy_fencing_rejection_never_marks_the_mission_delivered():
    """If the target's fencing check rejects the H2-approval-consumed token
    (StaleApproval) -- e.g. because a differently-sequenced release already
    pushed the environment's fencing state further ahead -- the orchestrator must
    NEVER assume the deploy happened. It escalates to AWAITING_HX instead of
    letting the mission drift toward OBSERVING/DELIVERED for a release that may
    never have actually reached production (red team #3 C1: "the older mission
    is not marked delivered without deploying")."""
    ctx = new_context()
    try:
        mission_id = run_feature_to_merged(
            ctx, title="A release whose deploy token will be stale",
            implementer_script=_implementer({"app/greet.py": fixtures.GREET_APP,
                                             "tests/test_greet.py": fixtures.GREET_TEST_PASS}),
            task_objective="Add a greet() helper with a passing unit test.")
        advance_post_merge_ok(ctx, mission_id)
        assert ctx.mission(mission_id).state == "AWAITING_H2"

        # Some other, unrelated deploy already pushed production's fencing state
        # far ahead of whatever token THIS mission's H2 consume will draw next.
        ctx.adapters["deploy"].deploy("sha256:unrelated", environment="production",
                                      operation_id="unrelated-op", fencing_token=999_999)

        ctx.decide(mission_id, "H2", "approve")  # consumes a real (but now-stale) token
        state = ctx.mission(mission_id).state
        assert state == "AWAITING_HX", f"expected AWAITING_HX on a fencing rejection, got {state}"
        events = [e["kind"] for e in ctx.events(mission_id)]
        assert "deploy_failed" in events
        assert "delivered" not in events
    finally:
        ctx.cleanup()


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

        import dataclasses

        raw_payload = {"action": "labeled",
                       "issue": {"number": 101, "title": "A patch labeled on 'serve', processed by 'worker'",
                                 "body": "please fix", "user": {"login": "@dev"}},
                       "label": {"name": "factory:patch"},
                       "repository": {"full_name": ctx.repo}}
        parsed = parse_event("issues", raw_payload)
        assert parsed is not None
        # app.py parses the raw GitHub JSON itself and enqueues the TYPED event's
        # class name + field dict (never the raw JSON) -- `dispatch_webhooks`
        # rebuilds `EventClass(**payload)` rather than re-parsing GitHub's body.
        kind = type(parsed).__name__
        payload = dataclasses.asdict(parsed)

        # "serve" only enqueues -- durably, in the shared store.
        assert serve.enqueue_webhook("delivery-1", kind, payload) is True
        assert serve.enqueue_webhook("delivery-1", kind, payload) is False, "a duplicate delivery must be a no-op"
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


# ── item 5 (M2): gateway/discovery keys are revoked on every pause/terminal ─────
def test_gateway_key_is_revoked_on_cancel_and_a_fresh_one_is_minted_on_resume():
    """H1 "decline" -> ARCHIVED must revoke the discovery key; separately, an
    ACTIVE mission that gets HELD (kill switch) and later resumed must never let
    an agent reuse the key that was revoked when it paused (self-healing
    `_gateway_key`, red team #3 item 5/M2)."""
    ctx = new_context()
    try:
        runtime = ctx.adapters["runtime"]
        runtime.set_script("intake", _intake("feature", "AC4"))
        runtime.set_script("architect", _architect(tasks=[task("T-1", "Add a greet() helper with a test.")]))
        item = ctx.add_work_item(title="A feature that will be declined at H1", body="...",
                                 labels=("factory:feature",))
        mission = ctx.start(item)
        assert mission.state == "AWAITING_H1"
        discovery_key = ctx.factory.recall(mission.mission_id, "discovery_key")
        assert discovery_key is not None

        ctx.decide(mission.mission_id, "H1", "cancel")
        assert ctx.mission(mission.mission_id).state == "ARCHIVED"
        assert ctx.adapters["budget"].is_revoked(ctx.factory._fernet.decrypt(discovery_key["key"].encode()).decode())
    finally:
        ctx.cleanup()


def test_gateway_key_is_revoked_on_kill_switch_and_reminted_on_resume():
    ctx = new_context()
    try:
        seen_keys = []
        runtime = ctx.adapters["runtime"]
        runtime.set_script("intake", _intake("patch", "AC3"))

        def implementer_script(request):
            seen_keys.append(request.gateway_key)
            return _implementer({"app/greet.py": fixtures.GREET_APP,
                                 "tests/test_greet.py": fixtures.GREET_TEST_PASS})(request)

        runtime.set_script("implementer", implementer_script)
        runtime.set_script("reviewer", _reviewer("pass"))
        item = ctx.add_work_item(title="A patch that will be kill-switched before it runs", body="...",
                                 labels=("factory:patch",))
        mission = ctx.start(item)
        assert mission.state == "ACTIVE", mission.state  # standing mandate: no H1
        cached = ctx.factory.recall(mission.mission_id, "gateway_key")
        assert cached is not None
        first_key = ctx.factory._fernet.decrypt(cached["key"].encode()).decode()

        ctx.factory.kill_switch()
        assert ctx.mission(mission.mission_id).state == "HELD"
        assert ctx.adapters["budget"].is_revoked(first_key), "kill_switch must revoke the mission's gateway key"

        ctx.factory.resume(mission.mission_id)
        # This mission was killed while plain ACTIVE (not at an AWAITING_* gate),
        # so per the FSM's dynamic "resume" it lands in AWAITING_HX -- a human
        # decision is required before any agent runs again either way.
        assert ctx.mission(mission.mission_id).state == "AWAITING_HX"
        ctx.decide(mission.mission_id, "HX", "approve")
        assert ctx.mission(mission.mission_id).state == "ACTIVE"

        ctx.factory.run_ready_tasks(mission.mission_id)
        assert seen_keys and seen_keys[0], "the mission must run its task after the HX approval"
        assert seen_keys[0] != first_key, "resuming must never reuse a revoked gateway key"
        assert not ctx.adapters["budget"].is_revoked(seen_keys[0])
    finally:
        ctx.cleanup()


# ── item 6 (M3): gateway keys are Fernet-encrypted at rest, never stored raw ────
def test_gateway_key_is_never_stored_raw_in_events():
    ctx = new_context()
    try:
        seen_keys = []
        runtime = ctx.adapters["runtime"]
        runtime.set_script("intake", _intake("patch", "AC3"))

        def implementer_script(request):
            seen_keys.append(request.gateway_key)
            return _implementer({"app/greet.py": fixtures.GREET_APP,
                                 "tests/test_greet.py": fixtures.GREET_TEST_PASS})(request)

        runtime.set_script("implementer", implementer_script)
        runtime.set_script("reviewer", _reviewer("pass"))
        item = ctx.add_work_item(title="A patch to check gateway-key encryption at rest", body="...",
                                 labels=("factory:patch",))
        mission = ctx.start(item)
        ctx.factory.run_ready_tasks(mission.mission_id)

        assert seen_keys and seen_keys[0], "the implementer must have run with a real gateway key"
        raw_key = seen_keys[0]

        cached = ctx.factory.recall(mission.mission_id, "gateway_key")
        assert cached is not None
        assert cached["key"] != raw_key, "the cached record must hold ciphertext, never the raw key"

        for event in ctx.factory.store.list_events(mission.mission_id):
            assert raw_key not in repr(event), f"the raw gateway key leaked into an event: {event['kind']}"
    finally:
        ctx.cleanup()


# ── item 9: admission also gates on the product's monthly_usd cost cap ─────────
def test_admission_stays_queued_when_the_monthly_budget_is_exhausted():
    ctx = new_context()
    try:
        from factory.telemetry import metrics as telemetry_metrics

        product = ctx.product
        monthly_cap = product.budgets["monthly_usd"]

        runtime = ctx.adapters["runtime"]
        runtime.set_script("intake", _intake("patch", "AC3"))
        first_item = ctx.add_work_item(title="An earlier patch that used up the monthly budget", body="...",
                                       labels=("factory:patch",))
        first = ctx.start(first_item)
        assert first.state == "ACTIVE"
        # Simulate the month's spend already having reached the cap (real usage
        # would get there via many `cost_spent` events; one big one is equivalent
        # for `_spent_this_month`'s purposes).
        ctx.factory.store.append_event(first.mission_id, telemetry_metrics.COST_SPENT, {"usd": monthly_cap})

        second_item = ctx.add_work_item(title="A patch admitted while the monthly budget is exhausted", body="...",
                                        labels=("factory:patch",))
        second = ctx.start(second_item)
        assert second.state == "ADMITTED", \
            f"expected the second mission to stay queued in ADMITTED, got {second.state}"
    finally:
        ctx.cleanup()


# ── item 11: kill_switch / resume / unblock entry points ────────────────────────
def test_kill_switch_pauses_and_unblock_resumes_via_a_fresh_hx_approval():
    ctx = new_context()
    try:
        runtime = ctx.adapters["runtime"]
        runtime.set_script("intake", _intake("feature", "AC4"))
        runtime.set_script("architect", _architect(tasks=[task("T-1", "Add a greet() helper with a test.")]))
        item = ctx.add_work_item(title="A feature that gets deferred at HX then unblocked", body="...",
                                 labels=("factory:feature",))
        mission = ctx.start(item)
        ctx.decide(mission.mission_id, "H1", "approve")
        assert ctx.mission(mission.mission_id).state == "ACTIVE"

        ctx.factory.kill_switch()
        assert ctx.mission(mission.mission_id).state == "HELD"

        # every other non-terminal mission is HELD too; a second kill_switch call
        # (e.g. an operator double-clicking) must be a harmless no-op.
        ctx.factory.kill_switch()
        assert ctx.mission(mission.mission_id).state == "HELD"

        ctx.factory.resume(mission.mission_id)
        assert ctx.mission(mission.mission_id).state == "AWAITING_HX"
        ctx.decide(mission.mission_id, "HX", "defer")
        assert ctx.mission(mission.mission_id).state == "BLOCKED"

        ctx.factory.unblock(mission.mission_id)
        request = ctx.open_request(mission.mission_id, "HX")
        approver = ctx.product.approvers_for("HX")[0]
        roles = ctx.product.roles_for(approver)
        ctx.factory.decide(request.request_id, approver, roles, "approve", request.content_hash)
        ctx.factory.process_approvals()
        assert ctx.mission(mission.mission_id).state == "ACTIVE"
    finally:
        ctx.cleanup()


# ── item 13/8: discovery failure and a failed follow-up action never get stuck ──
def test_discovery_failure_escalates_to_hx_instead_of_staying_stuck():
    ctx = new_context()
    try:
        runtime = ctx.adapters["runtime"]

        def broken_architect(request):
            from factory.models import RuntimeResult
            return RuntimeResult(session_id="broken-architect-session", status="completed",
                                 output_text="not json at all", usage_usd=0.01)

        runtime.set_script("intake", _intake("feature", "AC4"))
        runtime.set_script("architect", broken_architect)
        item = ctx.add_work_item(title="A feature whose architect output is garbage", body="...",
                                 labels=("factory:feature",))
        mission = ctx.start(item)
        assert mission.state == "AWAITING_HX", \
            f"a discovery failure must escalate to AWAITING_HX, never stay in DISCOVERING, got {mission.state}"
        events = [e["kind"] for e in ctx.events(mission.mission_id)]
        assert "approval_requested" in events
    finally:
        ctx.cleanup()


# ── item 14: DORA metrics aren't distorted by non-production events ────────────
def test_post_merge_revert_is_not_counted_as_a_production_incident():
    from factory.telemetry.metrics import compute_dora_metrics

    ctx = new_context()
    try:
        mission_id = run_feature_to_merged(
            ctx, title="Add a greeting helper (CI fails on main after merge)",
            implementer_script=_implementer({"app/greet.py": fixtures.GREET_APP,
                                             "tests/test_greet.py": fixtures.GREET_TEST_PASS}),
            task_objective="Add a greet() helper with a passing unit test.")
        merge_sha = ctx.merge_sha(mission_id)
        ctx.fail_check(merge_sha, failing="unit")
        ctx.fire_check_suite(merge_sha, conclusion="failure")
        events = ctx.factory.store.list_events(mission_id)
        assert any(e["kind"] == "reverted" for e in events)

        dora = compute_dora_metrics({mission_id: events})
        assert dora.change_fail_rate is None or dora.change_fail_rate == 0, \
            "a revert of a change that never reached production must not count toward change_fail_rate"
    finally:
        ctx.cleanup()


def test_push_to_default_only_affects_active_missions_in_that_repo():
    ctx = new_context()
    try:
        runtime = ctx.adapters["runtime"]
        runtime.set_script("intake", _intake("feature", "AC4"))
        runtime.set_script("architect", _architect(tasks=[task("T-1", "Add a greet() helper with a test.")]))
        awaiting_item = ctx.add_work_item(title="A feature still awaiting H1 when main gets pushed to",
                                          body="...", labels=("factory:feature",))
        awaiting_mission = ctx.start(awaiting_item)
        assert awaiting_mission.state == "AWAITING_H1"

        runtime.set_script("intake", _intake("patch", "AC3"))
        active_item = ctx.add_work_item(title="A patch that's ACTIVE when main gets pushed to", body="...",
                                        labels=("factory:patch",))
        active_mission = ctx.start(active_item)
        assert active_mission.state == "ACTIVE"

        from factory.github.webhooks import PushToDefault
        ctx.factory.on_push_to_default(PushToDefault(repo=ctx.repo, branch="main", after="deadbeef",
                                                      pusher="@random-human"))

        active_events = [e["kind"] for e in ctx.events(active_mission.mission_id)]
        awaiting_events = [e["kind"] for e in ctx.events(awaiting_mission.mission_id)]
        assert "human_intervention" in active_events
        assert "human_intervention" not in awaiting_events, \
            "a mission not currently ACTIVE has no live branch this push affects"
    finally:
        ctx.cleanup()
