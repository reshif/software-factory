"""`factory demo` (build spec §3 B7): run scenarios, or `--serve` for a live, local
app + worker with fakes so a human can approve through the browser."""
from __future__ import annotations

import logging
import threading
import time

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


def serve_demo(*, host: str = "127.0.0.1", port: int = 8080) -> None:
    """Start the app + a background worker with fakes, seed one feature mission
    awaiting H1, and print signed inbox links so a human can approve in a browser.
    """
    import sys
    import uvicorn

    # Line-buffer stdout: when this runs under Docker/redirected output, the
    # seeded-mission and inbox-link lines below must show up immediately, not
    # only whenever the process's stdout buffer happens to flush.
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except (AttributeError, ValueError):
        pass

    from ..app import create_app
    from ..inbox.signing import TokenSigner
    from ..worker import Worker
    from .harness import new_context

    ctx = new_context(clock=SystemClock())
    from .scenarios import _architect, _intake  # local demo scripting helpers, deliberately not part of the public API

    runtime = ctx.adapters["runtime"]
    runtime.set_script("intake", _intake("feature", "AC4"))
    runtime.set_script("architect", _architect(tasks=[{
        "task_id": "T-1", "objective": "Add a small greeting helper with a test.",
        "owned_paths": ["app/**", "tests/**"], "action_class": "AC4",
        "acceptance_checks": ["lint", "unit"], "depends_on": []}]))
    item = ctx.add_work_item(title="Add a greeting helper (serve demo)",
                             body="Seeded automatically by `factory demo --serve`.",
                             labels=("factory:feature",))
    mission = ctx.start(item)

    app = create_app(ctx.settings, factory=ctx.factory)
    worker = Worker(ctx.factory, clock=ctx.clock)
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            try:
                worker.run_once()
            except Exception:
                logger.exception("worker tick failed")
            stop.wait(2.0)

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()

    signer = TokenSigner(ctx.settings.inbox_signing_key)
    print(f"\nSeeded mission {mission.mission_id} in state {ctx.mission(mission.mission_id).state}.")
    print("Open approvals:")
    for request in ctx.factory.store.approvals.list_open():
        approver = ctx.product.approvers_for(request.gate)[0]
        roles = ctx.product.roles_for(approver)
        token = signer.issue(approver=approver, roles=roles, expires=request.expires, request_id=request.request_id)
        print(f"  {request.gate} as {approver}: http://{host}:{port}/inbox/{request.request_id}?token={token}")
    print(f"\nServing on http://{host}:{port} (Ctrl+C to stop)\n")

    try:
        uvicorn.run(app, host=host, port=port)
    finally:
        stop.set()
        ctx.cleanup()


__all__ = ["run_all", "run_scenario", "serve_demo"]
