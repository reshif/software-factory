"""`factory demo` (build spec §3 B7): run scenarios, or `--serve` for a live, local
app + worker with fakes so a human can approve through the browser."""
from __future__ import annotations

import logging
import threading
import time
from datetime import timedelta

from ..clock import SystemClock
from .scenarios import SCENARIOS

logger = logging.getLogger(__name__)


def run_scenario(name: str) -> bool:
    fn = SCENARIOS.get(name)
    if fn is None:
        print(f"unknown scenario {name!r}. Choices: {', '.join(sorted(SCENARIOS))}")
        return False
    print(f"\n########## scenario: {name} ##########")
    try:
        ok = fn()
    except Exception:
        logger.exception("scenario %s raised", name)
        ok = False
    print(f"result: {'PASS' if ok else 'FAIL'}")
    return ok


def run_all() -> bool:
    results = {name: run_scenario(name) for name in SCENARIOS}
    print("\n===== summary =====")
    for name, ok in results.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    total = len(results)
    passed = sum(results.values())
    print(f"{passed}/{total} scenarios passed")
    return all(results.values())


class _FastForwardClock(SystemClock):
    """Real time plus an offset the serve demo can bump, so the 24h observation
    window can be skipped during a browser session."""

    def __init__(self) -> None:
        self.offset = timedelta(0)

    def now(self):
        return super().now() + self.offset


def serve_demo(*, host: str = "127.0.0.1", port: int = 8080, observe_seconds: float = 20.0) -> None:
    """Start the app + a background worker on fakes, seed one feature mission, and
    let a human take it from H1 all the way to DELIVERED in a browser.

    Fakes stand in for GitHub and the agents: agents are scripted, CI "passes" on
    every pushed and merged commit (a simulated check_suite webhook), and once a
    mission has been OBSERVING for `observe_seconds` the clock is fast-forwarded past
    its observation window. Each time a new approval opens, a signed link is printed.
    """
    import sys
    import uvicorn

    # Line-buffer stdout so links show up immediately under Docker/redirected output.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    from ..app import create_app
    from ..inbox.signing import TokenSigner
    from ..worker import Worker
    from . import fixtures
    from .harness import new_context
    from .scenarios import CHECK_NAMES, _architect, _implementer, _intake, _reviewer

    clock = _FastForwardClock()
    ctx = new_context(clock=clock)
    runtime = ctx.adapters["runtime"]
    runtime.set_script("intake", _intake("feature", "AC4"))
    runtime.set_script("architect", _architect(tasks=[{
        "task_id": "T-1", "objective": "Add a small greeting helper with a test.",
        "owned_paths": ["app/**", "tests/**"], "action_class": "AC4",
        "acceptance_checks": ["lint", "unit"], "depends_on": []}]))
    runtime.set_script("implementer", _implementer({"app/greet.py": fixtures.GREET_APP,
                                                    "tests/test_greet.py": fixtures.GREET_TEST_PASS},
                                                   tests_added=["tests/test_greet.py"]))
    runtime.set_script("reviewer", _reviewer("pass"))
    item = ctx.add_work_item(title="Add a greeting helper (serve demo)",
                             body="Seeded automatically by `factory demo --serve`.",
                             labels=("factory:feature",))
    mission = ctx.start(item)

    app = create_app(ctx.settings, factory=ctx.factory)
    worker = Worker(ctx.factory, clock=clock)
    signer = TokenSigner(ctx.settings.inbox_signing_key)
    stop = threading.Event()
    printed: set[str] = set()
    ci_fired: set[str] = set()
    observing_since: dict[str, float] = {}
    last_state: dict[str, str] = {}

    def print_new_links() -> None:
        for request in ctx.factory.store.approvals.list_open():
            if request.request_id in printed:
                continue
            printed.add(request.request_id)
            approver = ctx.product.approvers_for(request.gate)[0]
            token = signer.issue(approver=approver, roles=ctx.product.roles_for(approver),
                                 expires=request.expires, request_id=request.request_id)
            print(f"  [{request.gate}] open as {approver}: "
                  f"http://{host}:{port}/inbox/{request.request_id}?token={token}")

    def simulate_github(m) -> None:
        """What GitHub Actions would do: report green CI for the pushed/merged commit."""
        for key in ("pending_auto_merge", "merge_sha"):
            cached = ctx.factory.recall(m.mission_id, key)
            sha = cached and cached.get("sha")
            if sha and sha not in ci_fired:
                ci_fired.add(sha)
                ctx.pass_checks(sha, CHECK_NAMES)
                ctx.fire_check_suite(sha)

    def loop() -> None:
        while not stop.is_set():
            try:
                worker.run_once()
                for m in ctx.factory.store.list_missions():
                    if last_state.get(m.mission_id) != m.state:
                        last_state[m.mission_id] = m.state
                        print(f"  mission {m.mission_id} -> {m.state}")
                    if m.state in ("INTEGRATING", "MERGED", "AWAITING_HM"):
                        simulate_github(m)
                    if m.state == "OBSERVING":
                        since = observing_since.setdefault(m.mission_id, time.monotonic())
                        if time.monotonic() - since >= observe_seconds:
                            clock.offset += timedelta(hours=25)
                            ctx.factory.advance_observation(ctx.mission(m.mission_id))
                            observing_since.pop(m.mission_id, None)
                print_new_links()
            except Exception:
                logger.exception("demo tick failed")
            stop.wait(2.0)

    print(f"\nSeeded mission {mission.mission_id}. Approve each gate by opening its link.")
    print("New links are printed here as gates open (H1 -> HM -> H2), then the mission is DELIVERED.")
    print_new_links()
    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    print(f"\nServing on http://{host}:{port} (Ctrl+C to stop)\n")

    try:
        # Inbox tokens travel in the URL query string: keep them out of the access log.
        uvicorn.run(app, host=host, port=port, access_log=False)
    finally:
        stop.set()
        ctx.cleanup()


__all__ = ["run_all", "run_scenario", "serve_demo"]
