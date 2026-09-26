"""In-memory StateStore (final draft §12.2): the reference adapter for tests and `factory demo`.

Approvals and intents are delegated to the reference implementations in
`factory.controller` — `ApprovalStore` and `IntentLog` — so their semantics
are exactly the ones `PostgresStateStore` must reproduce.
"""
import threading
from dataclasses import replace

from ..clock import SystemClock
from ..controller.approvals import ApprovalStore
from ..controller.intents import IntentLog
from ..models import DecisionPacket, EvidenceBundle, MissionRecord, TaskRecord
from ..ports import Clock, ConcurrentUpdate, NotFound
from .validation import MISSION_IMMUTABLE, TASK_IMMUTABLE, validate_update_fields


class MemoryStateStore:
    """Plain-Python `StateStore` (ports.StateStore). Not shared across processes.

    Missions and tasks are returned as copies (`dataclasses.replace`), both
    going in and coming out, so a caller can never mutate stored state by
    holding onto (and editing) a record it got from `create_*`/`get_*`/`list_*`
    (`Q-M1`).
    """

    def __init__(self, clock: Clock | None = None) -> None:
        self._clock = clock or SystemClock()
        self.approvals = ApprovalStore()
        self.intents = IntentLog()
        self._lock = threading.Lock()
        self._missions: dict[str, MissionRecord] = {}
        self._tasks: dict[str, TaskRecord] = {}
        self._evidence: dict[tuple[str, str], EvidenceBundle] = {}
        self._evidence_order: list[tuple[str, str]] = []
        self._packets: dict[str, DecisionPacket] = {}
        self._events: dict[str, list[dict]] = {}

    # ── missions ──────────────────────────────────────────────────────
    def create_mission(self, mission: MissionRecord) -> MissionRecord:
        with self._lock:
            if mission.mission_id in self._missions:
                raise ValueError(f"duplicate mission id {mission.mission_id}")
            now = self._clock.now()
            stored = replace(mission, created_at=now, updated_at=now)
            self._missions[mission.mission_id] = stored
            return replace(stored)

    def get_mission(self, mission_id: str) -> MissionRecord:
        with self._lock:
            try:
                return replace(self._missions[mission_id])
            except KeyError:
                raise NotFound(mission_id) from None

    def update_mission(self, mission_id: str, *, expected_version: int, **changes) -> MissionRecord:
        """CAS update: raises ConcurrentUpdate if state_version != expected_version."""
        validate_update_fields(MissionRecord, changes, immutable=MISSION_IMMUTABLE)
        with self._lock:
            current = self._missions.get(mission_id)
            if current is None:
                raise NotFound(mission_id)
            if current.state_version != expected_version:
                raise ConcurrentUpdate(
                    f"{mission_id}: expected version {expected_version}, found {current.state_version}")
            updated = replace(current, state_version=current.state_version + 1,
                              updated_at=self._clock.now(), **changes)
            self._missions[mission_id] = updated
            return replace(updated)

    def list_missions(self, *, state: str | None = None, product: str | None = None) -> list[MissionRecord]:
        with self._lock:
            return [replace(m) for m in self._missions.values()
                    if (state is None or m.state == state) and (product is None or m.product == product)]

    def find_mission_by_work_item(self, work_item_id: str) -> MissionRecord | None:
        with self._lock:
            for mission in self._missions.values():
                if mission.work_item_id == work_item_id:
                    return replace(mission)
            return None

    # ── tasks ─────────────────────────────────────────────────────────
    def create_task(self, task: TaskRecord) -> TaskRecord:
        with self._lock:
            if task.task_id in self._tasks:
                raise ValueError(f"duplicate task id {task.task_id}")
            stored = replace(task, updated_at=self._clock.now())
            self._tasks[task.task_id] = stored
            return replace(stored)

    def get_task(self, task_id: str) -> TaskRecord:
        with self._lock:
            try:
                return replace(self._tasks[task_id])
            except KeyError:
                raise NotFound(task_id) from None

    def update_task(self, task_id: str, **changes) -> TaskRecord:
        validate_update_fields(TaskRecord, changes, immutable=TASK_IMMUTABLE)
        with self._lock:
            current = self._tasks.get(task_id)
            if current is None:
                raise NotFound(task_id)
            updated = replace(current, updated_at=self._clock.now(), **changes)
            self._tasks[task_id] = updated
            return replace(updated)

    def list_tasks(self, mission_id: str) -> list[TaskRecord]:
        with self._lock:
            return [replace(t) for t in self._tasks.values() if t.mission_id == mission_id]

    # ── evidence & packets ───────────────────────────────────────────
    def save_evidence(self, bundle: EvidenceBundle) -> None:
        with self._lock:
            key = (bundle.mission_id, bundle.revision)
            if key not in self._evidence:
                self._evidence_order.append(key)
            self._evidence[key] = bundle

    def get_evidence(self, mission_id: str, revision: str | None = None) -> EvidenceBundle | None:
        with self._lock:
            if revision is not None:
                return self._evidence.get((mission_id, revision))
            for key in reversed(self._evidence_order):
                if key[0] == mission_id:
                    return self._evidence[key]
            return None

    def save_packet(self, packet: DecisionPacket) -> None:
        with self._lock:
            self._packets[packet.request_id] = packet

    def get_packet(self, request_id: str) -> DecisionPacket:
        with self._lock:
            try:
                return self._packets[request_id]
            except KeyError:
                raise NotFound(request_id) from None

    # ── events ───────────────────────────────────────────────────────
    def append_event(self, mission_id: str, kind: str, payload: dict) -> None:
        with self._lock:
            self._events.setdefault(mission_id, []).append(
                {"kind": kind, "payload": payload, "at": self._clock.now()})

    def list_events(self, mission_id: str) -> list[dict]:
        with self._lock:
            return list(self._events.get(mission_id, []))
