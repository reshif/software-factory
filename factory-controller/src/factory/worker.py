"""The background worker loop (build spec §3 B7).

`run_once` does exactly what the build spec lists: dispatch any webhook events
that were queued (fast) by the HTTP layer, apply side-state timeouts, release
READY tasks, reconcile pending intents, and advance OBSERVING missions. Agent
runs and GitHub/deploy calls all happen here, in a plain background loop --
never inline in an HTTP request handler (see `pipeline/orchestrator.py`'s
webhook queue and `app.py`).

**v1 timeout simplification.** `mission_fsm.TIMEOUTS` gives one deadline per
side state ("escalate to the backup approver first, then fire the event",
final draft §6.3). This worker notifies the backup approver and fires the
timeout event at the same deadline, rather than modeling two separate
deadlines the build spec doesn't specify durations for -- see
`factory-controller/README.md`.
"""
from __future__ import annotations

import logging
import time

from .controller import mission_fsm
from .pipeline.orchestrator import Factory
from .ports import ConcurrentUpdate

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, factory: Factory, *, clock=None):
        self._factory = factory
        self._clock = clock

    def run_once(self, now=None) -> None:
        now = now or self._now()
        self._factory.dispatch_webhooks()
        self._apply_timeouts(now)
        self._factory.run_ready_tasks()
        self._reconcile_intents()
        self._advance_observing(now)

    def _now(self):
        # Default to the factory's own clock, so the worker always agrees with
        # the orchestrator about "now" unless a test explicitly overrides it.
        return (self._clock or self._factory.clock).now()

    def _apply_timeouts(self, now) -> None:
        store = self._factory.store
        for state, (timeout, event) in mission_fsm.TIMEOUTS.items():
            for mission in store.list_missions(state=state):
                updated_at = mission.updated_at or now
                if now - updated_at < timeout:
                    continue
                marker = f"timeout_notice:{state}"
                if self._factory.recall(mission.mission_id, marker) is None:
                    product = self._factory.product_for(mission)
                    backup = product.owners.get("backup")
                    self._factory.notifier.info(
                        f"{mission.mission_id} has been {state} since {updated_at.isoformat()}; "
                        f"escalating to backup approver {backup}")
                    self._factory.remember(mission.mission_id, marker, {"at": now.isoformat()})
                try:
                    self._factory.transition(mission, event)
                except (mission_fsm.InvalidTransition, ConcurrentUpdate) as exc:
                    logger.warning("%s: timeout transition %s failed: %s", mission.mission_id, event, exc)

    def _reconcile_intents(self) -> None:
        """Best-effort visibility pass over intents still marked pending.

        Real replay would need a `probe` specific to each operation kind (does
        the PR already have this merge commit? is the artifact already
        deployed at this fencing token?). v1 logs pending intents for operator
        attention rather than guessing at a generic retry -- the side effects
        that most need exactly-once semantics (merge, deploy) are already
        protected independently: HM merges are gated by a single-use approval
        consume, and deploys are gated by the target's own fencing-token check.
        """
        for intent in self._factory.store.intents.pending():
            logger.warning("intent %s is still pending; see README 'v1 scope notes' on reconciliation",
                          intent.operation_id)

    def _advance_observing(self, now) -> None:
        for mission in self._factory.store.list_missions(state="OBSERVING"):
            elapsed = now - (mission.updated_at or now)
            product = self._factory.product_for(mission)
            if elapsed >= product.observation_window:
                self._factory.advance_observation(mission)

    def run_forever(self, interval_s: float = 5.0, *, sleep=time.sleep, iterations: int | None = None) -> None:
        count = 0
        while iterations is None or count < iterations:
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 -- the loop must survive a single bad tick
                logger.exception("worker tick failed")
            count += 1
            if iterations is None or count < iterations:
                sleep(interval_s)


__all__ = ["Worker"]
