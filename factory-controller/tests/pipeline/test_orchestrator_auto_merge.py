"""The webhook-driven "auto" HM path (final draft §6.2 gate table: AC1-3 are `auto`
on the `experimental` profile). None of the required demo scenarios reach this --
they all run at `standard` risk, where HM always needs an approval -- so it is
exercised here directly (build spec §3 B7 "Tool guard"/"HM" sections).
"""
from factory.demo.harness import new_context
from factory.demo.scenarios import CHECK_NAMES, _implementer, _intake, _reviewer
from factory.demo import fixtures


def test_patch_auto_merges_once_ci_is_green_on_the_pushed_commit():
    ctx = new_context(risk_profile="experimental")
    try:
        runtime = ctx.adapters["runtime"]
        runtime.set_script("intake", _intake("patch", "AC3"))
        runtime.set_script("implementer", _implementer({"app/greet.py": fixtures.GREET_APP,
                                                         "tests/test_greet.py": fixtures.GREET_TEST_PASS}))
        runtime.set_script("reviewer", _reviewer("pass"))

        item = ctx.add_work_item(title="A small patch that should auto-merge",
                                 body="Small enough for the experimental profile's auto gate.",
                                 labels=("factory:patch",))
        mission = ctx.start(item)
        assert mission.state == "ACTIVE"  # standing mandate: no H1
        ctx.factory.run_ready_tasks(mission.mission_id)

        # HM is "auto" at this risk profile: no approval request should exist,
        # and the mission waits in INTEGRATING for CI on the pushed commit.
        assert ctx.mission(mission.mission_id).state == "INTEGRATING"
        assert not ctx.factory.store.approvals.list_open(mission.mission_id)

        sha = ctx.pending_auto_merge_sha(mission.mission_id)
        ctx.pass_checks(sha, CHECK_NAMES)
        ctx.fire_check_suite(sha)

        assert ctx.mission(mission.mission_id).state == "MERGED"
    finally:
        ctx.cleanup()


def test_patch_auto_merge_waits_if_ci_is_not_yet_green():
    ctx = new_context(risk_profile="experimental")
    try:
        runtime = ctx.adapters["runtime"]
        runtime.set_script("intake", _intake("patch", "AC3"))
        runtime.set_script("implementer", _implementer({"app/greet.py": fixtures.GREET_APP,
                                                         "tests/test_greet.py": fixtures.GREET_TEST_PASS}))
        runtime.set_script("reviewer", _reviewer("pass"))

        item = ctx.add_work_item(title="A small patch whose CI hasn't finished yet",
                                 body="...", labels=("factory:patch",))
        mission = ctx.start(item)
        ctx.factory.run_ready_tasks(mission.mission_id)
        sha = ctx.pending_auto_merge_sha(mission.mission_id)

        ctx.fail_check(sha, failing="unit", others=CHECK_NAMES)
        ctx.fire_check_suite(sha, conclusion="failure")

        assert ctx.mission(mission.mission_id).state == "INTEGRATING", "must not merge on a failing check"
    finally:
        ctx.cleanup()
