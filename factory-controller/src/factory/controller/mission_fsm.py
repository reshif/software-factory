"""Mission state machine (final draft §14.1).

Every non-terminal state has an exit, every side state has a timeout, and
no worker runs while a mission is held, blocked or awaiting a human.
"""
from dataclasses import dataclass, field
from datetime import timedelta

TERMINAL = frozenset({"DELIVERED", "ARCHIVED", "CANCELED"})
AWAITING = frozenset({"AWAITING_H1", "AWAITING_HM", "AWAITING_H2", "AWAITING_HX"})
NO_WORKER = AWAITING | {"HELD", "BLOCKED"} | TERMINAL
WORK_STATES = frozenset({"ADMITTED", "ACTIVE", "INTEGRATING", "REVERTED", "RELEASE_READY"})

TRANSITIONS: dict[tuple[str, str], str] = {
    ("NEW", "not_covered"): "DISCOVERING",
    ("NEW", "covered"): "ADMITTED",
    ("DISCOVERING", "packet_ready"): "AWAITING_H1",
    ("AWAITING_H1", "approved"): "ADMITTED",
    ("AWAITING_H1", "revise"): "DISCOVERING",
    ("AWAITING_H1", "decline"): "ARCHIVED",
    ("AWAITING_H1", "timeout"): "HELD",
    ("ADMITTED", "capacity_ok"): "ACTIVE",
    ("ACTIVE", "tasks_done"): "INTEGRATING",
    ("ACTIVE", "coverage_exceeded"): "DISCOVERING",
    ("INTEGRATING", "hm_auto"): "MERGED",
    ("INTEGRATING", "hm_required"): "AWAITING_HM",
    ("INTEGRATING", "conflict"): "ACTIVE",
    ("AWAITING_HM", "approved"): "MERGED",
    ("AWAITING_HM", "changes_requested"): "ACTIVE",
    ("AWAITING_HM", "timeout"): "HELD",
    ("MERGED", "post_merge_failure"): "REVERTED",
    ("MERGED", "evidence_complete"): "RELEASE_READY",
    ("REVERTED", "repair"): "ACTIVE",
    ("RELEASE_READY", "h2_standing"): "DEPLOYING",
    ("RELEASE_READY", "h2_required"): "AWAITING_H2",
    ("RELEASE_READY", "evidence_failed"): "ACTIVE",
    ("AWAITING_H2", "approval_consumed"): "DEPLOYING",
    ("AWAITING_H2", "changes_requested"): "ACTIVE",
    ("AWAITING_H2", "defer"): "HELD",
    ("AWAITING_H2", "timeout"): "HELD",
    ("DEPLOYING", "deployed"): "OBSERVING",
    ("DEPLOYING", "deploy_failed"): "RECOVERING",
    ("OBSERVING", "window_healthy"): "DELIVERED",
    ("OBSERVING", "regression"): "RECOVERING",
    ("RECOVERING", "recovered"): "ACTIVE",
    ("RECOVERING", "outside_authority"): "AWAITING_HX",
    ("AWAITING_HX", "approved"): "ACTIVE",
    ("AWAITING_HX", "deferred"): "BLOCKED",
    ("AWAITING_HX", "declined"): "CANCELED",
    ("AWAITING_HX", "timeout"): "HELD",
    ("HELD", "hold_expired"): "CANCELED",
    ("BLOCKED", "hx_approved"): "ACTIVE",
    ("BLOCKED", "block_expired"): "CANCELED",
    # ("HELD", "resume") is resolved dynamically, see Mission.fire
}

# Events that apply from many states.
GLOBAL_EVENTS = {
    "mandate_expired": ("AWAITING_HX", WORK_STATES),
    "budget_exhausted": ("AWAITING_HX", WORK_STATES),
    # "boundary": an agent/approval follow-up action failed, or discovery itself
    # failed (invalid architect output, a runtime error) -- there is no safe
    # automated next step, so escalate to a human from ANY in-progress state,
    # not just ACTIVE (red team #3 items 8/L5 and 13: never leave a mission
    # stuck in ADMITTED/DISCOVERING/etc. with no way forward).
    "boundary": ("AWAITING_HX", WORK_STATES | {"DISCOVERING"}),
    "kill_switch": ("HELD", None),   # None = every non-terminal state except HELD
}

# Side-state timeouts: escalate to the backup approver first, then fire the event.
TIMEOUTS = {
    "AWAITING_H1": (timedelta(days=3), "timeout"),
    "AWAITING_HM": (timedelta(days=2), "timeout"),
    "AWAITING_H2": (timedelta(days=2), "timeout"),
    "AWAITING_HX": (timedelta(days=2), "timeout"),
    "HELD": (timedelta(days=30), "hold_expired"),
    "BLOCKED": (timedelta(days=30), "block_expired"),
}


class InvalidTransition(Exception):
    pass


def all_states() -> frozenset:
    states = {"NEW", "HELD"}
    for (src, _), dst in TRANSITIONS.items():
        states.update((src, dst))
    return frozenset(states)


@dataclass
class Mission:
    mission_id: str
    state: str = "NEW"
    held_from: str | None = None
    history: list = field(default_factory=list)

    @property
    def worker_allowed(self) -> bool:
        return self.state not in NO_WORKER

    def can(self, event: str) -> bool:
        try:
            self._target(event)
            return True
        except InvalidTransition:
            return False

    def fire(self, event: str) -> str:
        target = self._target(event)
        if target == "HELD":
            self.held_from = self.state
        elif self.state == "HELD":
            self.held_from = None
        self.history.append((self.state, event, target))
        self.state = target
        return target

    def _target(self, event: str) -> str:
        if self.state in TERMINAL:
            raise InvalidTransition(f"{self.mission_id} is terminal ({self.state})")
        if event in GLOBAL_EVENTS:
            target, sources = GLOBAL_EVENTS[event]
            if sources is None:
                if self.state != "HELD":
                    return target
            elif self.state in sources:
                return target
        if self.state == "HELD" and event == "resume":
            # Resuming a paused approval returns to it; anything else (e.g. after a kill switch)
            # needs a human exception before work continues.
            return self.held_from if self.held_from in AWAITING else "AWAITING_HX"
        try:
            return TRANSITIONS[(self.state, event)]
        except KeyError:
            raise InvalidTransition(f"{self.mission_id}: no transition from {self.state} on {event!r}") from None
