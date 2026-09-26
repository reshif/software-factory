"""Pipeline-level analogs of final draft §19.3 walkthroughs #7 and #8.

`tests/walkthroughs/test_walkthroughs.py` exercises these two guarantees
against the bare `controller.intents`/`controller.approvals` primitives, with
a hand-rolled "world" (a Python list) standing in for whatever the real side
effect touches. These versions exercise the SAME guarantees through the real
stack B7's wave assembles: an actual `Factory`, its real `StateStore`
(`store.intents`), a real `GitHubPort` adapter (`FakeGitHub`, the same one
`wiring.py` gives production its `RestGitHub` counterpart for), and -- for #7
specifically -- a **second, independently constructed `Factory` on the same
store**, standing in for the controller process restarting after a crash.
"""
from __future__ import annotations

from factory.controller import mission_fsm
from factory.controller.intents import execute_once
from factory.demo.harness import new_context
from factory.demo.scenarios import _architect, _intake, task
from factory.wiring import build_factory


# ── walkthrough #7: crash between intent and receipt has no duplicate effect ────
def test_07_crash_between_intent_and_receipt_reconciles_on_a_new_factory_instance():
    ctx = new_context()
    try:
        github = ctx.adapters["github"]
        repo, number = ctx.repo, 1
        ctx.add_work_item(title="a work item to comment on", labels=())  # registers issue #1 in FakeGitHub
        comment = "the factory pushed revision sha256:deadbeef"
        op_id = "comment:MIS-crash:sha256:deadbeef"

        def post_comment():
            # The real effect: GitHub actually receives this comment...
            github.comment_issue(repo, number, comment)
            # ...but the controller crashes before it can record the receipt
            # (final draft §10: write-ahead intent, THEN act, THEN receipt).
            raise RuntimeError("controller crashed before writing the receipt")

        crashed = False
        try:
            execute_once(ctx.factory.store.intents, op_id, action=post_comment, probe=lambda: None)
        except RuntimeError:
            crashed = True
        assert crashed, "the simulated crash must actually propagate"
        assert github.comments(repo, number).count(comment) == 1, "the real effect happened exactly once"
        assert ctx.factory.store.intents.get(op_id).status == "pending", (
            "no receipt was written -- the intent must still read as pending")

        # "Restart": a brand-new Factory, built independently, on the SAME store
        # and the SAME GitHub adapter (a fresh Factory doesn't get a fresh world --
        # only a fresh in-process cache).
        restarted_factory = build_factory(
            ctx.settings, clock=ctx.clock,
            adapters={**ctx.adapters, "store": ctx.factory.store, "github": github})
        assert restarted_factory is not ctx.factory

        def post_comment_again():
            github.comment_issue(repo, number, comment)
            return "posted-again"

        def probe_already_posted():
            # A probe that inspects the REAL target, not the (lost) in-process
            # cache -- exactly what makes reconciliation possible after an actual
            # process crash, where nothing but the store and the external system
            # survive.
            return "already-posted" if comment in github.comments(repo, number) else None

        receipt = execute_once(restarted_factory.store.intents, op_id, action=post_comment_again,
                               probe=probe_already_posted)

        assert receipt == "already-posted", "reconciliation must find the prior effect, not re-run the action"
        assert github.comments(repo, number).count(comment) == 1, "still exactly one comment: no duplicate effect"
        assert restarted_factory.store.intents.get(op_id).status == "done"
        # And the ORIGINAL Factory's own view of the same (shared) store agrees.
        assert ctx.factory.store.intents.get(op_id).status == "done"
    finally:
        ctx.cleanup()


# ── walkthrough #8: an approval timeout never approves ──────────────────────────
def test_08_approval_timeout_notifies_backup_then_holds_never_approves():
    from factory.worker import Worker

    ctx = new_context()
    try:
        runtime = ctx.adapters["runtime"]
        runtime.set_script("intake", _intake("feature", "AC4"))
        runtime.set_script("architect", _architect(tasks=[task("T-1", "Add a small helper.")]))
        item = ctx.add_work_item(title="A change awaiting H1", labels=("factory:feature",))
        mission = ctx.start(item)
        assert mission.state == "AWAITING_H1"
        request = ctx.open_request(mission.mission_id, "H1")
        assert request.status == "open"

        timeout, event = mission_fsm.TIMEOUTS["AWAITING_H1"]
        assert event == "timeout", "final draft §6.3: a side-state timeout only ever fires 'timeout'"
        ctx.clock.advance(seconds=timeout.total_seconds() + 1)

        Worker(ctx.factory, clock=ctx.clock).run_once()

        held = ctx.mission(mission.mission_id)
        assert held.state == "HELD", "a timed-out approval must never advance the mission as if approved"
        backup = ctx.product.owners["backup"]
        assert any(mission.mission_id in msg and backup in msg for msg in ctx.adapters["notifier"].info_messages), (
            "the backup approver must be notified before/at the timeout, final draft §6.3")
        # It never reached ADMITTED (H1 approved) or any state past AWAITING_H1.
        assert held.state not in ("ADMITTED", "ACTIVE")
    finally:
        ctx.cleanup()
