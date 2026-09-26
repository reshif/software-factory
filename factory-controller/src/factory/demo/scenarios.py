"""The 11 end-to-end demo scenarios from build spec §3 B7.

Every scenario builds its own `DemoContext` (a temp product repo + a fully
local `Factory`), scripts `FakeRuntime` per role, drives the mission through
`Factory`/webhook calls exactly as `app.py`/`worker.py` would, prints a
readable timeline, and asserts its expected end state. `tests/e2e/` runs every
one of these as a pytest test.
"""
from __future__ import annotations

import logging

from ..controller.approvals import AlreadyConsumed
from ..runtime.fake import scripted
from . import fixtures
from .harness import DemoContext, new_context

logger = logging.getLogger(__name__)

CHECK_NAMES = ("lint", "unit", "review_agent")
FEATURE_OWNED = ("app/**", "tests/**")


# ── scripting helpers ────────────────────────────────────────────────────────────
def _intake(lane: str, suggested_class: str, summary: str = "Triaged by the demo intake script."):
    return scripted(output={"lane": lane, "suggested_class": suggested_class, "summary": summary, "risk_notes": []})


def _architect(tasks: list, *, summary: str = "A small, scoped change.",
              recommendation: str = "APPROVE: scoped, testable, no protected paths touched.",
              recovery_plan: str = "Kill the mission flag; no data impact.", estimate_usd: float = 5.0):
    return scripted(output={"summary": summary, "recommendation": recommendation,
                            "acceptance_criteria": ["WHEN the change ships THE SYSTEM SHALL behave as specified."],
                            "tasks": tasks, "alternatives": [], "risks": [], "recovery_plan": recovery_plan,
                            "estimate_usd": estimate_usd})


def _implementer(files: dict, *, status: str = "done", summary: str = "Implemented the change.",
                 tests_added: list | None = None, blocker: str | None = None):
    return scripted(output={"status": status, "summary": summary, "tests_added": tests_added or [],
                            "commands_run": [], "blocker": blocker}, files=files)


def _reviewer(verdict: str = "pass", findings: list | None = None):
    return scripted(output={"verdict": verdict, "findings": findings or []})


def _sequence(*scripts):
    """A `Script` that plays `scripts` in order, repeating the last one once exhausted."""
    state = {"i": 0}

    def run(request):
        i = min(state["i"], len(scripts) - 1)
        state["i"] += 1
        return scripts[i](request)

    return run


def task(task_id: str, objective: str, *, owned_paths=FEATURE_OWNED, action_class: str = "AC4",
        acceptance_checks=CHECK_NAMES, depends_on: tuple = ()) -> dict:
    return {"task_id": task_id, "objective": objective, "owned_paths": list(owned_paths),
           "action_class": action_class, "acceptance_checks": list(acceptance_checks),
           "depends_on": list(depends_on)}


# ── flow helpers, shared across scenarios ───────────────────────────────────────
def advance_to_merged(ctx: DemoContext, mission_id: str) -> None:
    m = ctx.mission(mission_id)
    if m.state == "AWAITING_HM":
        ctx.decide(mission_id, "HM", "approve")
    elif m.state == "INTEGRATING":
        sha = ctx.pending_auto_merge_sha(mission_id)
        ctx.pass_checks(sha, CHECK_NAMES)
        ctx.fire_check_suite(sha)
    m = ctx.mission(mission_id)
    assert m.state == "MERGED", f"expected MERGED, got {m.state}"


def advance_post_merge_ok(ctx: DemoContext, mission_id: str) -> None:
    sha = ctx.merge_sha(mission_id)
    ctx.pass_checks(sha, CHECK_NAMES)
    ctx.fire_check_suite(sha)


def advance_h2_to_observing(ctx: DemoContext, mission_id: str) -> None:
    m = ctx.mission(mission_id)
    if m.state == "AWAITING_H2":
        ctx.decide(mission_id, "H2", "approve")
    m = ctx.mission(mission_id)
    assert m.state == "OBSERVING", f"expected OBSERVING, got {m.state}"


def deliver(ctx: DemoContext, mission_id: str) -> None:
    ctx.clock.advance(hours=25)
    ctx.factory.advance_observation(ctx.mission(mission_id))


def run_feature_to_merged(ctx: DemoContext, *, title: str, implementer_script, task_objective: str) -> str:
    runtime = ctx.adapters["runtime"]
    runtime.set_script("intake", _intake("feature", "AC4"))
    runtime.set_script("architect", _architect(tasks=[task("T-1", task_objective)]))
    runtime.set_script("implementer", implementer_script)
    runtime.set_script("reviewer", _reviewer("pass"))

    item = ctx.add_work_item(title=title, body="Please make this change.", labels=("factory:feature",))
    mission = ctx.start(item)
    assert mission.state == "AWAITING_H1", mission.state
    ctx.decide(mission.mission_id, "H1", "approve")
    assert ctx.mission(mission.mission_id).state == "ACTIVE"
    ctx.factory.run_ready_tasks(mission.mission_id)
    advance_to_merged(ctx, mission.mission_id)
    return mission.mission_id


# ── 1: happy_path ────────────────────────────────────────────────────────────────
def happy_path() -> bool:
    ctx = new_context()
    try:
        mission_id = run_feature_to_merged(
            ctx, title="Add a greeting helper endpoint",
            implementer_script=_implementer({"app/greet.py": fixtures.GREET_APP,
                                             "tests/test_greet.py": fixtures.GREET_TEST_PASS},
                                            tests_added=["tests/test_greet.py"]),
            task_objective="Add a greet() helper with a passing unit test.")
        advance_post_merge_ok(ctx, mission_id)
        advance_h2_to_observing(ctx, mission_id)
        deliver(ctx, mission_id)
        ctx.print_timeline(mission_id, title="happy_path")
        ok = ctx.mission(mission_id).state == "DELIVERED"
        assert ok, f"expected DELIVERED, got {ctx.mission(mission_id).state}"
        return True
    finally:
        ctx.cleanup()


# ── 2: patch_standing (covered patch; auto/1 gates per the table) ───────────────
def patch_standing() -> bool:
    ctx = new_context()
    try:
        runtime = ctx.adapters["runtime"]
        runtime.set_script("intake", _intake("patch", "AC3"))
        runtime.set_script("implementer", _implementer({"app/store.py.notes": fixtures.PATCH_NOTE}))
        runtime.set_script("reviewer", _reviewer("pass"))

        item = ctx.add_work_item(title="Small doc clarification for the store module",
                                 body="Please clarify a comment.", labels=("factory:patch",))
        mission = ctx.start(item)
        assert mission.state == "ACTIVE", mission.state  # standing mandate: no H1
        ctx.factory.run_ready_tasks(mission.mission_id)
        advance_to_merged(ctx, mission.mission_id)
        advance_post_merge_ok(ctx, mission.mission_id)
        advance_h2_to_observing(ctx, mission.mission_id)
        deliver(ctx, mission.mission_id)
        ctx.print_timeline(mission.mission_id, title="patch_standing")
        ok = ctx.mission(mission.mission_id).state == "DELIVERED"
        assert ok, f"expected DELIVERED, got {ctx.mission(mission.mission_id).state}"
        return True
    finally:
        ctx.cleanup()


# ── 3: coverage_exceeded ─────────────────────────────────────────────────────────
def coverage_exceeded() -> bool:
    ctx = new_context()
    try:
        runtime = ctx.adapters["runtime"]
        runtime.set_script("intake", _intake("patch", "AC3"))
        big = fixtures.BIG_MODULE_TEMPLATE.format(body=fixtures.big_module_body(200))
        runtime.set_script("implementer", _implementer({"app/generated_padding.py": big}))
        runtime.set_script("architect", _architect(tasks=[task("T-1", "Re-plan after coverage was exceeded.",
                                                               action_class="AC3")]))

        item = ctx.add_work_item(title="Small patch that turns out to be too big",
                                 body="Should be small.", labels=("factory:patch",))
        mission = ctx.start(item)
        assert mission.state == "ACTIVE", mission.state
        ctx.factory.run_ready_tasks(mission.mission_id)
        ctx.print_timeline(mission.mission_id, title="coverage_exceeded")
        state = ctx.mission(mission.mission_id).state
        assert state == "AWAITING_H1", f"expected AWAITING_H1, got {state}"
        return True
    finally:
        ctx.cleanup()


# ── 4: h1_reject (revise, then decline -> ARCHIVED) ─────────────────────────────
def h1_reject() -> bool:
    ctx = new_context()
    try:
        runtime = ctx.adapters["runtime"]
        runtime.set_script("intake", _intake("feature", "AC4"))
        runtime.set_script("architect", _architect(tasks=[task("T-1", "A feature the humans will reject.")]))

        item = ctx.add_work_item(title="A feature nobody actually wants", body="Speculative work.",
                                 labels=("factory:feature",))
        mission = ctx.start(item)
        assert mission.state == "AWAITING_H1", mission.state
        ctx.decide(mission.mission_id, "H1", "revise")
        assert ctx.mission(mission.mission_id).state == "AWAITING_H1"  # a fresh round, re-discovered
        ctx.decide(mission.mission_id, "H1", "cancel")
        ctx.print_timeline(mission.mission_id, title="h1_reject")
        state = ctx.mission(mission.mission_id).state
        assert state == "ARCHIVED", f"expected ARCHIVED, got {state}"
        return True
    finally:
        ctx.cleanup()


# ── 5: repair_then_pass ──────────────────────────────────────────────────────────
def repair_then_pass() -> bool:
    ctx = new_context()
    try:
        broken = _implementer({"app/greet.py": fixtures.GREET_APP_BROKEN,
                               "tests/test_greet.py": fixtures.GREET_TEST_PASS})
        fixed = _implementer({"app/greet.py": fixtures.GREET_APP_FIXED,
                              "tests/test_greet.py": fixtures.GREET_TEST_PASS})
        mission_id = run_feature_to_merged(ctx, title="Add a greeting helper (will need one repair)",
                                           implementer_script=_sequence(broken, fixed),
                                           task_objective="Add a greet() helper with a passing unit test.")
        advance_post_merge_ok(ctx, mission_id)
        advance_h2_to_observing(ctx, mission_id)
        deliver(ctx, mission_id)
        ctx.print_timeline(mission_id, title="repair_then_pass")
        tasks = ctx.factory.store.list_tasks(mission_id)
        assert any(t.repair_attempts_used >= 1 for t in tasks), "expected at least one repair attempt"
        ok = ctx.mission(mission_id).state == "DELIVERED"
        assert ok, f"expected DELIVERED, got {ctx.mission(mission_id).state}"
        return True
    finally:
        ctx.cleanup()


# ── 6: repair_exhausted_hx ───────────────────────────────────────────────────────
def repair_exhausted_hx() -> bool:
    ctx = new_context()
    try:
        runtime = ctx.adapters["runtime"]
        runtime.set_script("intake", _intake("feature", "AC4"))
        runtime.set_script("architect", _architect(tasks=[task("T-1", "A change that never passes checks.")]))
        runtime.set_script("implementer", _implementer({"app/greet.py": fixtures.GREET_APP_BROKEN,
                                                        "tests/test_greet.py": fixtures.GREET_TEST_PASS}))

        item = ctx.add_work_item(title="A change that keeps failing its own test",
                                 body="Never actually fixed.", labels=("factory:feature",))
        mission = ctx.start(item)
        ctx.decide(mission.mission_id, "H1", "approve")
        ctx.factory.run_ready_tasks(mission.mission_id)
        ctx.print_timeline(mission.mission_id, title="repair_exhausted_hx")
        state = ctx.mission(mission.mission_id).state
        assert state == "AWAITING_HX", f"expected AWAITING_HX, got {state}"
        tasks = ctx.factory.store.list_tasks(mission.mission_id)
        assert any(t.state == "FAILED" for t in tasks)
        return True
    finally:
        ctx.cleanup()


# ── 7: forbidden_path_blocked ────────────────────────────────────────────────────
def forbidden_path_blocked() -> bool:
    ctx = new_context()
    try:
        runtime = ctx.adapters["runtime"]
        runtime.set_script("intake", _intake("feature", "AC4"))
        runtime.set_script("architect", _architect(
            tasks=[task("T-1", "Silence CI so a bug slips through.",
                       owned_paths=(".github/workflows/**",))]))
        runtime.set_script("implementer", _implementer({".github/workflows/ci.yml": fixtures.FORBIDDEN_WORKFLOW_EDIT}))

        item = ctx.add_work_item(title="Adjust the CI workflow (this must be blocked)",
                                 body="Trying to touch CI.", labels=("factory:feature",))
        mission = ctx.start(item)
        ctx.decide(mission.mission_id, "H1", "approve")
        ctx.factory.run_ready_tasks(mission.mission_id)
        ctx.print_timeline(mission.mission_id, title="forbidden_path_blocked")
        state = ctx.mission(mission.mission_id).state
        assert state == "AWAITING_HX", f"expected AWAITING_HX, got {state}"
        assert mission.pr_number is None, "an AC8 diff must never reach a pushed branch/PR"
        return True
    finally:
        ctx.cleanup()


# ── 8: post_merge_revert ─────────────────────────────────────────────────────────
def post_merge_revert() -> bool:
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
        ctx.print_timeline(mission_id, title="post_merge_revert")
        state = ctx.mission(mission_id).state
        assert state == "ACTIVE", f"expected ACTIVE (post-revert repair), got {state}"
        events = [e["kind"] for e in ctx.events(mission_id)]
        assert "reverted" in events and "recovered" in events
        tasks = ctx.factory.store.list_tasks(mission_id)
        assert any("repair-" in t.task_id and t.state == "READY" for t in tasks)
        return True
    finally:
        ctx.cleanup()


# ── 9: prod_regression_rollback ──────────────────────────────────────────────────
def prod_regression_rollback() -> bool:
    ctx = new_context()
    try:
        mission_id = run_feature_to_merged(
            ctx, title="Add a greeting helper (will regress in production)",
            implementer_script=_implementer({"app/greet.py": fixtures.GREET_APP,
                                             "tests/test_greet.py": fixtures.GREET_TEST_PASS}),
            task_objective="Add a greet() helper with a passing unit test.")
        advance_post_merge_ok(ctx, mission_id)
        advance_h2_to_observing(ctx, mission_id)
        ctx.adapters["deploy"].set_healthy("production", False)
        deliver(ctx, mission_id)
        ctx.print_timeline(mission_id, title="prod_regression_rollback")
        state = ctx.mission(mission_id).state
        assert state == "AWAITING_HX", f"expected AWAITING_HX, got {state}"
        receipts = ctx.adapters["deploy"].receipts
        assert any(r.status == "rolled_back" for r in receipts), "expected a rollback receipt"
        flag = ctx.mission(mission_id).flag
        assert ctx.adapters["flags"].rollout(flag) == 0, "the flag must be killed on regression"
        return True
    finally:
        ctx.cleanup()


# ── 10: stale_approval_rejected ──────────────────────────────────────────────────
def stale_approval_rejected() -> bool:
    ctx = new_context()
    try:
        runtime = ctx.adapters["runtime"]
        runtime.set_script("intake", _intake("feature", "AC4"))
        runtime.set_script("architect", _architect(tasks=[task("T-1", "A feature whose H1 goes stale.")]))

        item = ctx.add_work_item(title="A feature whose approval will go stale", body="...",
                                 labels=("factory:feature",))
        mission = ctx.start(item)
        assert mission.state == "AWAITING_H1"
        request = ctx.open_request(mission.mission_id, "H1")
        # Something else advances the mission's state_version between the H1 request
        # being created and the approver's decision -- e.g. an unrelated CAS update.
        current = ctx.mission(mission.mission_id)
        ctx.factory.store.update_mission(mission.mission_id, expected_version=current.state_version,
                                         editors=("@someone-else",))
        # `decide()` only enqueues; `process_approvals()` (the worker's job) does the
        # actual consume and swallows a StaleApproval as an unmet-quorum-style no-op
        # (build spec §3 B7 "decide" red-team fix) -- so the observable effect is what
        # this asserts, not a raised exception.
        ctx.decide(mission.mission_id, "H1", "approve")
        ctx.print_timeline(mission.mission_id, title="stale_approval_rejected")
        refreshed = ctx.factory.store.approvals.get(request.request_id)
        assert refreshed.status == "open", f"a rejected consume must not mark the approval consumed, got {refreshed.status}"
        assert ctx.mission(mission.mission_id).state == "AWAITING_H1", "a rejected consume must not advance the mission"
        return True
    finally:
        ctx.cleanup()


# ── 11: replay_after_rollback ────────────────────────────────────────────────────
def replay_after_rollback() -> bool:
    ctx = new_context()
    try:
        mission_id = run_feature_to_merged(
            ctx, title="Add a greeting helper (H2 will be replayed after rollback)",
            implementer_script=_implementer({"app/greet.py": fixtures.GREET_APP,
                                             "tests/test_greet.py": fixtures.GREET_TEST_PASS}),
            task_objective="Add a greet() helper with a passing unit test.")
        advance_post_merge_ok(ctx, mission_id)
        h2_request = ctx.open_request(mission_id, "H2")
        advance_h2_to_observing(ctx, mission_id)
        ctx.adapters["deploy"].set_healthy("production", False)
        deliver(ctx, mission_id)
        assert ctx.mission(mission_id).state == "AWAITING_HX"

        try:
            ctx.factory.store.approvals.consume(
                h2_request.request_id, executor="replay-attempt", now=ctx.clock.now(),
                state_version=ctx.mission(mission_id).state_version, content_hash=h2_request.content_hash,
                policy_version=h2_request.policy_version)
            raised = False
        except AlreadyConsumed:
            raised = True
        ctx.print_timeline(mission_id, title="replay_after_rollback")
        assert raised, "replaying an already-consumed H2 approval must be rejected"
        return True
    finally:
        ctx.cleanup()


# ── 12: human_push_recorded ───────────────────────────────────────────────────────
def human_push_recorded() -> bool:
    """A human pushes directly to a mission's `factory/*` branch (final draft §7 step
    13's human-intervention signal, `github.webhooks.PushToBranch`). Recorded as a
    `human_intervention` event, and the pusher becomes a mission editor so the
    no-self-approval rule would exclude them from approving this mission's HM."""
    ctx = new_context()
    try:
        runtime = ctx.adapters["runtime"]
        runtime.set_script("intake", _intake("feature", "AC4"))
        runtime.set_script("architect", _architect(tasks=[task("T-1", "Add a greet() helper with a test.")]))
        runtime.set_script("implementer", _implementer({"app/greet.py": fixtures.GREET_APP,
                                                        "tests/test_greet.py": fixtures.GREET_TEST_PASS}))
        runtime.set_script("reviewer", _reviewer("pass"))

        item = ctx.add_work_item(title="Add a greeting helper (a human will push to its branch)",
                                 body="Please add a greeting helper.", labels=("factory:feature",))
        mission = ctx.start(item)
        ctx.decide(mission.mission_id, "H1", "approve")
        ctx.factory.run_ready_tasks(mission.mission_id)
        mission = ctx.mission(mission.mission_id)
        assert mission.state == "AWAITING_HM", mission.state
        assert mission.branch, "expected a pushed mission branch"

        from ..github.webhooks import PushToBranch
        ctx.factory.on_push_to_branch(PushToBranch(repo=ctx.repo, branch=mission.branch, sha="deadbeefcafe1234",
                                                    pusher="@random-human", pusher_is_bot=False))
        ctx.print_timeline(mission.mission_id, title="human_push_recorded")
        events = [e["kind"] for e in ctx.events(mission.mission_id)]
        assert "human_intervention" in events, "expected a human_intervention event"
        mission = ctx.mission(mission.mission_id)
        assert "@random-human" in mission.editors, "the human pusher must be recorded as an editor"
        return True
    finally:
        ctx.cleanup()


SCENARIOS = {
    "happy_path": happy_path,
    "patch_standing": patch_standing,
    "coverage_exceeded": coverage_exceeded,
    "h1_reject": h1_reject,
    "repair_then_pass": repair_then_pass,
    "repair_exhausted_hx": repair_exhausted_hx,
    "forbidden_path_blocked": forbidden_path_blocked,
    "post_merge_revert": post_merge_revert,
    "prod_regression_rollback": prod_regression_rollback,
    "stale_approval_rejected": stale_approval_rejected,
    "replay_after_rollback": replay_after_rollback,
    "human_push_recorded": human_push_recorded,
}

__all__ = ["SCENARIOS", *SCENARIOS.keys()]
