"""Regression tests for the B7 red-team #3 findings (final fix wave, orchestrator core).

Each test targets one finding from the third integration-layer red-team pass;
see `factory-controller/README.md` for the fix each one guards.
"""
from factory.controller.approvals import IneligibleApprover
from factory.demo import fixtures
from factory.demo.harness import new_context
from factory.demo.scenarios import _architect, _implementer, _intake, task


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
