"""A minimal `StateStore` stand-in for B4's own tests.

B1's `MemoryStateStore` is being built in parallel, so the inbox router — which only
ever touches `store.approvals` and `store.get_packet`/`save_packet` — gets a tiny
stub instead. It reuses the reference `ApprovalStore` so approval semantics (quorum,
expiry, single-use consume, revise/cancel voiding) are exactly the ones B1 will also
provide, per `factory.controller.approvals`. Everything else `StateStore` declares is
out of scope for the inbox and is left unimplemented on purpose.
"""
from __future__ import annotations

from factory.controller.approvals import ApprovalStore
from factory.controller.intents import IntentLog
from factory.models import DecisionPacket
from factory.ports import NotFound


class StubStateStore:
    """Implements just enough of `StateStore` for inbox router tests."""

    def __init__(self) -> None:
        self.approvals = ApprovalStore()
        self.intents = IntentLog()
        self._packets: dict[str, DecisionPacket] = {}
        self.events: list[tuple[str, str, dict]] = []

    def save_packet(self, packet: DecisionPacket) -> None:
        self._packets[packet.request_id] = packet

    def get_packet(self, request_id: str) -> DecisionPacket:
        try:
            return self._packets[request_id]
        except KeyError as exc:
            raise NotFound(request_id) from exc

    def append_event(self, mission_id: str, kind: str, payload: dict) -> None:
        self.events.append((mission_id, kind, payload))

    def list_events(self, mission_id: str) -> list[dict]:
        return [payload for mid, _kind, payload in self.events if mid == mission_id]

    # The rest of StateStore (missions, tasks, evidence) is out of scope for the inbox.
    def create_mission(self, mission):
        raise NotImplementedError("not needed by the inbox router")

    def get_mission(self, mission_id: str):
        raise NotImplementedError("not needed by the inbox router")

    def update_mission(self, mission_id: str, *, expected_version: int, **changes):
        raise NotImplementedError("not needed by the inbox router")

    def list_missions(self, *, state: str | None = None, product: str | None = None):
        raise NotImplementedError("not needed by the inbox router")

    def find_mission_by_work_item(self, work_item_id: str):
        raise NotImplementedError("not needed by the inbox router")

    def create_task(self, task):
        raise NotImplementedError("not needed by the inbox router")

    def get_task(self, task_id: str):
        raise NotImplementedError("not needed by the inbox router")

    def update_task(self, task_id: str, **changes):
        raise NotImplementedError("not needed by the inbox router")

    def list_tasks(self, mission_id: str):
        raise NotImplementedError("not needed by the inbox router")

    def save_evidence(self, bundle) -> None:
        raise NotImplementedError("not needed by the inbox router")

    def get_evidence(self, mission_id: str, revision: str | None = None):
        raise NotImplementedError("not needed by the inbox router")


__all__ = ["StubStateStore"]
