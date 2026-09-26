"""Regression tests for the B7 red-team #2 findings (orchestrator gap wave).

Each test targets one finding from the red-team review of the integration
layer; see `factory-controller/README.md` for the fix each one guards.
"""
from factory.demo import fixtures
from factory.demo.harness import new_context
from factory.demo.scenarios import (CHECK_NAMES, _architect, _implementer, _intake, _reviewer,
                                    advance_h2_to_observing, advance_post_merge_ok, advance_to_merged,
                                    run_feature_to_merged, task)
from factory.github.webhooks import PullRequestReview


# ── item 1: operation ids are keyed by revision, not just by mission ────────────
def test_repaired_revision_after_a_revert_is_actually_redeployed():
    ctx = new_context()
    try:
        mission_id = run_feature_to_merged(
            ctx, title="Add a greeting helper (CI fails on main; then gets repaired)",
            implementer_script=_implementer({"app/greet.py": fixtures.GREET_APP,
                                             "tests/test_greet.py": fixtures.GREET_TEST_PASS}),
            task_objective="Add a greet() helper with a passing unit test.")
        first_merge_sha = ctx.merge_sha(mission_id)
        ctx.fail_check(first_merge_sha, failing="unit")
        ctx.fire_check_suite(first_merge_sha, conclusion="failure")
        assert ctx.mission(mission_id).state == "ACTIVE"

        # Run the repair task the revert left READY, with a script that actually
        # "fixes" it (a distinct file, so the repaired diff has a different content
        # hash) -- then re-integrate, re-approve HM, and go all the way to a second
        # production deploy.
        ctx.adapters["runtime"].set_script("implementer", _implementer({"app/fix_note.md": "fixed\n"}))
        ctx.factory.run_ready_tasks(mission_id)
        advance_to_merged(ctx, mission_id)
        second_merge_sha = ctx.merge_sha(mission_id)
        assert second_merge_sha != first_merge_sha, "the repaired revision must be a genuinely new push/merge"

        advance_post_merge_ok(ctx, mission_id)
        advance_h2_to_observing(ctx, mission_id)
        ctx.clock.advance(hours=25)
        ctx.factory.advance_observation(ctx.mission(mission_id))
        assert ctx.mission(mission_id).state == "DELIVERED"

        receipts = ctx.adapters["deploy"].receipts
        prod_artifacts = {r.artifact for r in receipts if r.environment == "production" and r.status == "deployed"}
        assert len(prod_artifacts) == 1, (
            "expected exactly one production artifact deployed for the repaired "
            f"revision (op ids keyed by mission alone would have replayed the first "
            f"receipt instead), got {prod_artifacts}")
        assert not ctx.factory.store.intents.pending(), "no intent should be left pending"
    finally:
        ctx.cleanup()


# ── item 3: intake/architect get a real discovery budget key ────────────────────
def test_discovery_agents_get_a_real_gateway_key():
    ctx = new_context()
    try:
        seen_keys = []
        runtime = ctx.adapters["runtime"]

        def intake_script(request):
            seen_keys.append(("intake", request.gateway_key))
            return _intake("feature", "AC4")(request)

        def architect_script(request):
            seen_keys.append(("architect", request.gateway_key))
            return _architect(tasks=[task("T-1", "Add a greet() helper with a test.")])(request)

        runtime.set_script("intake", intake_script)
        runtime.set_script("architect", architect_script)

        item = ctx.add_work_item(title="A feature that needs discovery", body="...",
                                 labels=("factory:feature",))
        mission = ctx.start(item)
        assert mission.state == "AWAITING_H1"

        assert len(seen_keys) == 2
        for role, key in seen_keys:
            assert key, f"{role} ran with no gateway key at all"
            assert key.startswith("fake-key-"), f"{role} did not get a real budget-gateway key: {key!r}"

        # The discovery key is revoked once the mission is admitted (a real budget
        # key takes over) -- FakeBudgetGateway.is_revoked lets us check directly.
        ctx.decide(mission.mission_id, "H1", "approve")
        discovery_key = seen_keys[0][1]
        assert ctx.adapters["budget"].is_revoked(discovery_key)
        return
    finally:
        ctx.cleanup()


# ── item 4: captured diffs are confined to owned_paths, no symlinks ─────────────
def test_implementer_writing_outside_owned_paths_is_rejected():
    ctx = new_context()
    try:
        runtime = ctx.adapters["runtime"]
        runtime.set_script("intake", _intake("feature", "AC4"))
        runtime.set_script("architect", _architect(tasks=[task("T-1", "A task that oversteps its owned paths.",
                                                               owned_paths=("app/**",))]))
        # Writes outside app/** (owned_paths), even though the path itself is
        # otherwise unremarkable -- this must be caught by the scope check, not
        # slip through as an accepted diff.
        runtime.set_script("implementer", _implementer({"specs/not_owned.md": "sneaky content\n"}))

        item = ctx.add_work_item(title="A task that writes outside its owned paths", body="...",
                                 labels=("factory:feature",))
        mission = ctx.start(item)
        ctx.decide(mission.mission_id, "H1", "approve")
        ctx.factory.run_ready_tasks(mission.mission_id)

        state = ctx.mission(mission.mission_id).state
        assert state == "AWAITING_HX", f"expected AWAITING_HX (scope violation escalates), got {state}"
        tasks = ctx.factory.store.list_tasks(mission.mission_id)
        assert any(t.state == "FAILED" for t in tasks)
    finally:
        ctx.cleanup()


# ── item 6: auto-merge/post-merge require every check NAME, not just present ones ─
def test_auto_merge_waits_for_a_missing_required_check_name():
    ctx = new_context(risk_profile="experimental")
    try:
        runtime = ctx.adapters["runtime"]
        runtime.set_script("intake", _intake("patch", "AC3"))
        runtime.set_script("implementer", _implementer({"app/greet.py": fixtures.GREET_APP,
                                                        "tests/test_greet.py": fixtures.GREET_TEST_PASS}))
        runtime.set_script("reviewer", _reviewer("pass"))

        item = ctx.add_work_item(title="A patch whose CI only reports lint so far", body="...",
                                 labels=("factory:patch",))
        mission = ctx.start(item)
        ctx.factory.run_ready_tasks(mission.mission_id)
        sha = ctx.pending_auto_merge_sha(mission.mission_id)

        # Only "lint" reported so far; "unit" (and review_agent, not a GitHub check)
        # hasn't run yet. Reporting just the PRESENT check as green must not merge.
        ctx.pass_checks(sha, ("lint",))
        ctx.fire_check_suite(sha)
        assert ctx.mission(mission.mission_id).state == "INTEGRATING", \
            "must not auto-merge while a required check name has not reported at all"

        ctx.pass_checks(sha, ("lint", "unit"))
        ctx.fire_check_suite(sha)
        assert ctx.mission(mission.mission_id).state == "MERGED"
    finally:
        ctx.cleanup()


# ── item 2: HM is bound to the pushed/reviewed sha, and to a listed approver ─────
def test_pr_review_on_a_different_sha_is_ignored():
    ctx = new_context()
    try:
        runtime = ctx.adapters["runtime"]
        runtime.set_script("intake", _intake("feature", "AC4"))
        runtime.set_script("architect", _architect(tasks=[task("T-1", "Add a greet() helper with a test.")]))
        runtime.set_script("implementer", _implementer({"app/greet.py": fixtures.GREET_APP,
                                                        "tests/test_greet.py": fixtures.GREET_TEST_PASS}))
        runtime.set_script("reviewer", _reviewer("pass"))

        item = ctx.add_work_item(title="Add a greet() helper", body="...", labels=("factory:feature",))
        mission = ctx.start(item)
        ctx.decide(mission.mission_id, "H1", "approve")
        ctx.factory.run_ready_tasks(mission.mission_id)
        mission = ctx.mission(mission.mission_id)
        assert mission.state == "AWAITING_HM"

        # A review on a sha that was never pushed for this mission -- must be ignored.
        ctx.factory.on_pull_request_review(PullRequestReview(
            repo=ctx.repo, number=mission.pr_number, reviewer="@po", state="APPROVED", commit_id="not-the-pushed-sha"))
        assert ctx.mission(mission.mission_id).state == "AWAITING_HM", "a review on the wrong sha must not merge"

        # A review from someone who isn't a listed HM approver -- also ignored.
        pushed_sha = ctx.factory.recall(mission.mission_id, "pushed_sha")["sha"]
        ctx.factory.on_pull_request_review(PullRequestReview(
            repo=ctx.repo, number=mission.pr_number, reviewer="@not-an-approver", state="APPROVED",
            commit_id=pushed_sha))
        assert ctx.mission(mission.mission_id).state == "AWAITING_HM", "a review from a non-approver must not merge"

        # The real reviewer, on the real pushed sha, on the other hand, merges it.
        ctx.factory.on_pull_request_review(PullRequestReview(
            repo=ctx.repo, number=mission.pr_number, reviewer="@tl", state="APPROVED", commit_id=pushed_sha))
        assert ctx.mission(mission.mission_id).state == "MERGED"
    finally:
        ctx.cleanup()


# ── item 7: decide() only enqueues; it never advances the mission by itself ──────
def test_decide_alone_does_not_advance_the_mission():
    ctx = new_context()
    try:
        runtime = ctx.adapters["runtime"]
        runtime.set_script("intake", _intake("feature", "AC4"))
        runtime.set_script("architect", _architect(tasks=[task("T-1", "A feature awaiting H1.")]))

        item = ctx.add_work_item(title="A feature awaiting H1", body="...", labels=("factory:feature",))
        mission = ctx.start(item)
        request = ctx.open_request(mission.mission_id, "H1")

        ctx.factory.decide(request.request_id, "@po", ("product",), "approve", request.content_hash)
        assert ctx.mission(mission.mission_id).state == "AWAITING_H1", \
            "decide() must only enqueue -- the mission advances on process_approvals(), not before"

        ctx.factory.process_approvals()
        assert ctx.mission(mission.mission_id).state == "ACTIVE"
    finally:
        ctx.cleanup()
