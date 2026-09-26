"""Postgres-backed StateStore (final draft §10, §12.1, §12.2).

Approvals and intents must behave exactly like the in-memory reference
implementations in `factory.controller` (`ApprovalStore`, `IntentLog`): the
approval rules themselves (`check_decision`, `check_consumable`) are pure
functions shared by both (`C1`), so this module only has to load a row,
lock it, call the rule, and persist the result. The contract tests under
`tests/store/` run the same test functions against both backends to keep
them in sync.

Every public method opens its own short-lived connection/transaction. This
keeps CAS updates and the atomic approval-consume simple: `SELECT ... FOR
UPDATE` locks the row for the lifetime of that one transaction, and the
`with` block commits on success or rolls back on any exception.
"""
from __future__ import annotations

import importlib.resources
import json
import logging
from dataclasses import asdict, replace
from pathlib import Path

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from ..clock import SystemClock
from ..controller.approvals import ApprovalError, ApprovalRequest, Decision, check_consumable, check_decision
from ..controller.intents import Intent
from ..models import (CheckResult, DecisionPacket, DeployReceipt, EvidenceBundle, ExecResult,
                     MissionRecord, RuntimeResult, SandboxHandle, TaskRecord)
from ..policy import Requirement
from ..ports import Clock, ConcurrentUpdate, NotFound
from .validation import MISSION_IMMUTABLE, TASK_IMMUTABLE, validate_update_fields

logger = logging.getLogger(__name__)

_MIGRATIONS_PACKAGE = "factory.store.migrations"
# An arbitrary, stable key namespacing this lock; only used to serialize
# `apply_migrations` runs, never anything else, so no collision risk.
_MIGRATION_LOCK_KEY = 0x0FAC7051


# ── migrations ──────────────────────────────────────────────────────────────
def _load_migration_files(migrations_dir: Path | str | None = None) -> list[tuple[str, str]]:
    """Return `[(filename, sql), ...]` in filename order.

    Reads from `migrations_dir` if given (tests only); otherwise from the
    `factory.store.migrations` package via `importlib.resources`, so the SQL
    ships inside the installed package instead of depending on a checkout
    layout (`Q-H5`).
    """
    if migrations_dir is not None:
        paths = sorted(Path(migrations_dir).glob("*.sql"))
        return [(p.name, p.read_text()) for p in paths]
    package = importlib.resources.files(_MIGRATIONS_PACKAGE)
    entries = sorted((p for p in package.iterdir() if p.name.endswith(".sql")), key=lambda p: p.name)
    return [(p.name, p.read_text()) for p in entries]


def apply_migrations(conn: psycopg.Connection, migrations_dir: Path | str | None = None) -> list[str]:
    """Apply every migration file, in filename order, at most once.

    Idempotent: already-applied files (tracked in `schema_migrations`) are
    skipped, and each file's own SQL uses `IF NOT EXISTS` / `ON CONFLICT` so
    re-running the same file is also harmless. Raises `RuntimeError` if no
    `*.sql` files are found at all — an empty migration set almost always
    means the package wasn't installed correctly, not that there's nothing
    to do.

    A Postgres advisory lock serializes concurrent callers (e.g. two
    controller instances starting at once), so they can't race each other
    inserting the same row into `schema_migrations`.
    """
    files = _load_migration_files(migrations_dir)
    if not files:
        raise RuntimeError(
            f"no migration files found in {migrations_dir or _MIGRATIONS_PACKAGE!r}; "
            "the store package looks broken or incompletely installed")

    with conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_lock(%s)", (_MIGRATION_LOCK_KEY,))
    try:
        with conn.cursor() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "  filename TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())")
            cur.execute("SELECT filename FROM schema_migrations")
            applied = {row[0] for row in cur.fetchall()}
        conn.commit()

        newly_applied = []
        for name, sql in files:
            if name in applied:
                continue
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute("INSERT INTO schema_migrations (filename) VALUES (%s)", (name,))
            conn.commit()
            newly_applied.append(name)
            logger.info("applied migration %s", name)
        return newly_applied
    finally:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_advisory_unlock(%s)", (_MIGRATION_LOCK_KEY,))
        conn.commit()


# ── receipts (opaque application objects, e.g. a DeployReceipt or a plain str) ──
# JSON only, never pickle: unpickling data pulled back out of our own database is an
# unsafe-deserialization risk (anyone who can write the DB gets code execution), which is
# exactly what the security floor's fail-closed rule (§13.1) rules out. Dataclasses are the
# one non-JSON-native shape we support, and only from this explicit allowlist; anything else
# is rejected at complete() time instead of silently guessed at.
_RECEIPT_TYPES: dict[str, type] = {
    f"{cls.__module__}.{cls.__qualname__}": cls
    for cls in (DeployReceipt, CheckResult, ExecResult, SandboxHandle, RuntimeResult)
}


def _encode_receipt(receipt: object) -> str | None:
    if receipt is None:
        return None
    key = f"{type(receipt).__module__}.{type(receipt).__qualname__}"
    if key in _RECEIPT_TYPES:
        return json.dumps({"__type__": key, "data": asdict(receipt)})
    try:
        return json.dumps({"json": receipt})
    except TypeError:
        allowed = ", ".join(sorted(_RECEIPT_TYPES))
        raise TypeError(
            f"cannot store intent receipt of type {type(receipt).__name__!r}: it is neither "
            f"JSON-native nor one of the allowlisted factory.models dataclasses ({allowed})"
        ) from None


def _decode_receipt(raw: str | None) -> object:
    if raw is None:
        return None
    doc = json.loads(raw)
    if "json" in doc:
        return doc["json"]
    cls = _RECEIPT_TYPES[doc["__type__"]]
    return cls(**doc["data"])


# ── mission / task / evidence / packet (de)serialization ────────────────────
def _mission_params(mission: MissionRecord) -> dict:
    return {
        "mission_id": mission.mission_id, "product": mission.product, "repo": mission.repo,
        "work_item_id": mission.work_item_id, "lane": mission.lane, "risk_profile": mission.risk_profile,
        "autonomy_level": mission.autonomy_level, "kit_version": mission.kit_version,
        "policy_version": mission.policy_version, "state": mission.state,
        "state_version": mission.state_version, "held_from": mission.held_from,
        "action_class": mission.action_class, "mandate_id": mission.mandate_id,
        "budget_usd": mission.budget_usd, "spent_usd": mission.spent_usd,
        "base_commit": mission.base_commit, "branch": mission.branch, "pr_number": mission.pr_number,
        "content_hash": mission.content_hash, "artifact": mission.artifact, "flag": mission.flag,
        "editors": Json(list(mission.editors)), "created_at": mission.created_at,
        "updated_at": mission.updated_at,
    }


def _row_to_mission(row: dict) -> MissionRecord:
    return MissionRecord(
        mission_id=row["mission_id"], product=row["product"], repo=row["repo"],
        work_item_id=row["work_item_id"], lane=row["lane"], risk_profile=row["risk_profile"],
        autonomy_level=row["autonomy_level"], kit_version=row["kit_version"],
        policy_version=row["policy_version"], state=row["state"], state_version=row["state_version"],
        held_from=row["held_from"], action_class=row["action_class"], mandate_id=row["mandate_id"],
        budget_usd=row["budget_usd"], spent_usd=row["spent_usd"], base_commit=row["base_commit"],
        branch=row["branch"], pr_number=row["pr_number"], content_hash=row["content_hash"],
        artifact=row["artifact"], flag=row["flag"], editors=tuple(row["editors"] or ()),
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


def _task_params(task: TaskRecord) -> dict:
    return {
        "task_id": task.task_id, "mission_id": task.mission_id, "contract": Json(task.contract),
        "state": task.state, "depends_on": Json(list(task.depends_on)),
        "repair_attempts_used": task.repair_attempts_used, "infra_retries_used": task.infra_retries_used,
        "session_id": task.session_id, "revision": task.revision, "spent_usd": task.spent_usd,
        "updated_at": task.updated_at,
    }


def _row_to_task(row: dict) -> TaskRecord:
    return TaskRecord(
        task_id=row["task_id"], mission_id=row["mission_id"], contract=row["contract"],
        state=row["state"], depends_on=tuple(row["depends_on"] or ()),
        repair_attempts_used=row["repair_attempts_used"], infra_retries_used=row["infra_retries_used"],
        session_id=row["session_id"], revision=row["revision"], spent_usd=row["spent_usd"],
        updated_at=row["updated_at"],
    )


def _evidence_to_dict(bundle: EvidenceBundle) -> dict:
    return {
        "mission_id": bundle.mission_id, "revision": bundle.revision, "content_hash": bundle.content_hash,
        "diff_ref": bundle.diff_ref,
        "checks": {name: {k: v for k, v in asdict(c).items() if k != "name"}
                   for name, c in bundle.checks.items()},
        "action_class": bundle.action_class, "rule_fired": bundle.rule_fired,
        "task_ids": list(bundle.task_ids),
        "holdout": list(bundle.holdout) if bundle.holdout is not None else None,
        "blast_radius": list(bundle.blast_radius), "rollback_plan": bundle.rollback_plan,
        "rollback_tested": bundle.rollback_tested, "cost_usd": bundle.cost_usd, "tokens": bundle.tokens,
        "ci_minutes": bundle.ci_minutes, "untrusted_inputs": list(bundle.untrusted_inputs),
        "agent_versions": dict(bundle.agent_versions),
    }


def _evidence_from_dict(d: dict) -> EvidenceBundle:
    checks = {name: CheckResult(name=name, **fields) for name, fields in d["checks"].items()}
    holdout = tuple(d["holdout"]) if d.get("holdout") is not None else None
    return EvidenceBundle(
        mission_id=d["mission_id"], revision=d["revision"], content_hash=d["content_hash"],
        diff_ref=d["diff_ref"], checks=checks, action_class=d["action_class"],
        rule_fired=d["rule_fired"], task_ids=tuple(d.get("task_ids") or ()), holdout=holdout,
        blast_radius=tuple(d.get("blast_radius") or ()), rollback_plan=d.get("rollback_plan"),
        rollback_tested=bool(d.get("rollback_tested", False)), cost_usd=d.get("cost_usd", 0.0),
        tokens=d.get("tokens", 0), ci_minutes=d.get("ci_minutes", 0.0),
        untrusted_inputs=tuple(d.get("untrusted_inputs") or ()),
        agent_versions=dict(d.get("agent_versions") or {}),
    )


def _evidence_row_params(bundle: EvidenceBundle) -> dict:
    d = _evidence_to_dict(bundle)
    return {
        "mission_id": d["mission_id"], "revision": d["revision"], "content_hash": d["content_hash"],
        "diff_ref": d["diff_ref"], "checks": Json(d["checks"]), "action_class": d["action_class"],
        "rule_fired": d["rule_fired"], "task_ids": Json(d["task_ids"]),
        "holdout": Json(d["holdout"]) if d["holdout"] is not None else None,
        "blast_radius": Json(d["blast_radius"]), "rollback_plan": d["rollback_plan"],
        "rollback_tested": d["rollback_tested"], "cost_usd": d["cost_usd"], "tokens": d["tokens"],
        "ci_minutes": d["ci_minutes"], "untrusted_inputs": Json(d["untrusted_inputs"]),
        "agent_versions": Json(d["agent_versions"]),
    }


def _row_to_evidence(row: dict) -> EvidenceBundle:
    return _evidence_from_dict({
        "mission_id": row["mission_id"], "revision": row["revision"], "content_hash": row["content_hash"],
        "diff_ref": row["diff_ref"], "checks": row["checks"], "action_class": row["action_class"],
        "rule_fired": row["rule_fired"], "task_ids": row["task_ids"], "holdout": row["holdout"],
        "blast_radius": row["blast_radius"], "rollback_plan": row["rollback_plan"],
        "rollback_tested": row["rollback_tested"], "cost_usd": row["cost_usd"], "tokens": row["tokens"],
        "ci_minutes": row["ci_minutes"], "untrusted_inputs": row["untrusted_inputs"],
        "agent_versions": row["agent_versions"],
    })


def _packet_params(packet: DecisionPacket) -> dict:
    return {
        "request_id": packet.request_id, "gate": packet.gate, "mission_ids": Json(list(packet.mission_ids)),
        "title": packet.title, "recommendation": packet.recommendation, "summary": packet.summary,
        "required": packet.required, "expires": packet.expires, "content_hash": packet.content_hash,
        "raw_diff": packet.raw_diff, "alternatives": Json(list(packet.alternatives)),
        "evidence": Json(_evidence_to_dict(packet.evidence)) if packet.evidence is not None else None,
        "recovery_plan": packet.recovery_plan, "cost_usd": packet.cost_usd,
        "untrusted_inputs": Json(list(packet.untrusted_inputs)), "links": Json(dict(packet.links)),
    }


def _row_to_packet(row: dict) -> DecisionPacket:
    return DecisionPacket(
        request_id=row["request_id"], gate=row["gate"], mission_ids=tuple(row["mission_ids"]),
        title=row["title"], recommendation=row["recommendation"], summary=row["summary"],
        required=row["required"], expires=row["expires"], content_hash=row["content_hash"],
        raw_diff=row["raw_diff"], alternatives=tuple(row["alternatives"] or ()),
        evidence=_evidence_from_dict(row["evidence"]) if row["evidence"] is not None else None,
        recovery_plan=row["recovery_plan"], cost_usd=row["cost_usd"],
        untrusted_inputs=tuple(row["untrusted_inputs"] or ()), links=dict(row["links"] or {}),
    )


# ── approvals ────────────────────────────────────────────────────────────────
def _approval_params(req: ApprovalRequest) -> dict:
    return {
        "request_id": req.request_id, "gate": req.gate, "mission_ids": Json(list(req.mission_ids)),
        "operation_id": req.operation_id, "artifact": req.artifact, "content_hash": req.content_hash,
        "policy_version": req.policy_version, "state_version": req.state_version,
        "required_kind": req.required.kind, "required_approvals": req.required.approvals,
        "required_security": req.required.security, "required_sampled": req.required.sampled,
        "risk_profile": req.risk_profile, "requester": req.requester, "expires": req.expires,
        "editors": Json(sorted(req.editors)), "nonce": req.nonce, "status": req.status,
        "consumed_by": req.consumed_by, "consumed_at": req.consumed_at, "fencing_token": req.fencing_token,
    }


def _row_to_approval(row: dict, decisions: list[Decision]) -> ApprovalRequest:
    return ApprovalRequest(
        request_id=row["request_id"], gate=row["gate"], mission_ids=tuple(row["mission_ids"]),
        operation_id=row["operation_id"], artifact=row["artifact"], content_hash=row["content_hash"],
        policy_version=row["policy_version"], state_version=row["state_version"],
        required=Requirement(kind=row["required_kind"], approvals=row["required_approvals"],
                             security=row["required_security"], sampled=row["required_sampled"]),
        risk_profile=row["risk_profile"], requester=row["requester"], expires=row["expires"],
        editors=frozenset(row["editors"] or ()), nonce=row["nonce"], decisions=decisions,
        status=row["status"], consumed_by=row["consumed_by"], consumed_at=row["consumed_at"],
        fencing_token=row["fencing_token"],
    )


def _insert_decision(cur, request_id: str, approver: str, roles: frozenset, decision: str, at) -> None:
    cur.execute(
        "INSERT INTO approval_decisions (request_id, approver, roles, decision, decided_at) "
        "VALUES (%s, %s, %s, %s, %s)",
        (request_id, approver, Json(sorted(roles)), decision, at),
    )


def _load_decisions(cur, request_id: str) -> list[Decision]:
    cur.execute(
        "SELECT approver, roles, decision, decided_at FROM approval_decisions "
        "WHERE request_id = %s ORDER BY id",
        (request_id,),
    )
    return [Decision(approver=r["approver"], roles=frozenset(r["roles"]), decision=r["decision"],
                     at=r["decided_at"])
            for r in cur.fetchall()]


class _PostgresApprovalStore:
    """`ApprovalStorePort` over Postgres. Mirrors `controller.approvals.ApprovalStore`.

    Every method takes its own row lock (`SELECT ... FOR UPDATE`) for the
    duration of one transaction, which is what makes `consume()` atomic:
    the fencing token comes from a Postgres sequence, so it stays globally
    monotonic even across controller restarts.
    """

    def __init__(self, store: "PostgresStateStore") -> None:
        self._store = store

    def add(self, request: ApprovalRequest) -> ApprovalRequest:
        with self._store._connect() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "INSERT INTO approvals (request_id, gate, mission_ids, operation_id, artifact, "
                        "content_hash, policy_version, state_version, required_kind, required_approvals, "
                        "required_security, required_sampled, risk_profile, requester, expires, editors, "
                        "nonce, status, consumed_by, consumed_at, fencing_token) "
                        "VALUES (%(request_id)s, %(gate)s, %(mission_ids)s, %(operation_id)s, "
                        "%(artifact)s, %(content_hash)s, %(policy_version)s, %(state_version)s, "
                        "%(required_kind)s, %(required_approvals)s, %(required_security)s, "
                        "%(required_sampled)s, %(risk_profile)s, %(requester)s, %(expires)s, "
                        "%(editors)s, %(nonce)s, %(status)s, %(consumed_by)s, %(consumed_at)s, "
                        "%(fencing_token)s)",
                        _approval_params(request),
                    )
                except psycopg.errors.UniqueViolation:
                    raise ApprovalError(f"duplicate request id {request.request_id}") from None
                for d in request.decisions:
                    _insert_decision(cur, request.request_id, d.approver, d.roles, d.decision, d.at)
        return request

    def get(self, request_id: str) -> ApprovalRequest:
        with self._store._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT * FROM approvals WHERE request_id = %s", (request_id,))
                row = cur.fetchone()
                if row is None:
                    raise NotFound(request_id)
                decisions = _load_decisions(cur, request_id)
        return _row_to_approval(row, decisions)

    def list_open(self, mission_id: str | None = None) -> list[ApprovalRequest]:
        with self._store._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                if mission_id is None:
                    cur.execute("SELECT * FROM approvals WHERE status = 'open' ORDER BY id")
                else:
                    cur.execute(
                        "SELECT * FROM approvals WHERE status = 'open' AND mission_ids @> %s::jsonb "
                        "ORDER BY id",
                        (Json([mission_id]),),
                    )
                rows = cur.fetchall()
                result = []
                for row in rows:
                    decisions = _load_decisions(cur, row["request_id"])
                    result.append(_row_to_approval(row, decisions))
        return result

    def decide(self, request_id: str, *, approver: str, roles, decision: str, content_hash: str,
              now) -> ApprovalRequest:
        """Loads, locks, calls `check_decision` (the one place the rule lives) and persists."""
        with self._store._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT * FROM approvals WHERE request_id = %s FOR UPDATE", (request_id,))
                row = cur.fetchone()
                if row is None:
                    raise NotFound(request_id)
                req = _row_to_approval(row, _load_decisions(cur, request_id))
                d = check_decision(req, approver=approver, roles=roles, decision=decision,
                                   content_hash=content_hash, now=now)
                if d is None:
                    return req  # idempotent duplicate
                _insert_decision(cur, request_id, d.approver, d.roles, d.decision, d.at)
                req.decisions.append(d)
                if d.decision in ("revise", "cancel"):
                    cur.execute("UPDATE approvals SET status = 'void' WHERE request_id = %s", (request_id,))
                    req.status = "void"
                return req

    def consume(self, request_id: str, *, executor: str, now, state_version: int, content_hash: str,
               policy_version: str) -> int:
        """Loads, locks, calls `check_consumable` (the one place the rule lives) and persists.

        The fencing token comes from a Postgres sequence instead of an in-memory
        counter, so it stays globally monotonic across controller restarts.
        """
        with self._store._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT * FROM approvals WHERE request_id = %s FOR UPDATE", (request_id,))
                row = cur.fetchone()
                if row is None:
                    raise NotFound(request_id)
                req = _row_to_approval(row, _load_decisions(cur, request_id))
                check_consumable(req, now=now, state_version=state_version, content_hash=content_hash,
                                policy_version=policy_version)
                cur.execute("SELECT nextval('approval_fencing_seq') AS token")
                token = cur.fetchone()["token"]
                cur.execute(
                    "UPDATE approvals SET status = 'consumed', consumed_by = %s, consumed_at = %s, "
                    "fencing_token = %s WHERE request_id = %s",
                    (executor, now, token, request_id),
                )
        return token

    def invalidate_for_artifact(self, artifact: str, reason: str) -> list[str]:
        """Rollback: every unconsumed approval for this artifact becomes invalid."""
        logger.info("invalidating open approvals for artifact %s: %s", artifact, reason)
        with self._store._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE approvals SET status = 'invalidated' "
                    "WHERE artifact = %s AND status = 'open' RETURNING request_id",
                    (artifact,),
                )
                hit = [r[0] for r in cur.fetchall()]
        return hit


class _PostgresIntentLog:
    """`IntentLogPort` over Postgres. Mirrors `controller.intents.IntentLog`."""

    def __init__(self, store: "PostgresStateStore") -> None:
        self._store = store

    def get(self, operation_id: str) -> Intent | None:
        with self._store._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT status, receipt FROM intents WHERE operation_id = %s", (operation_id,))
                row = cur.fetchone()
        if row is None:
            return None
        return Intent(operation_id=operation_id, status=row["status"], receipt=_decode_receipt(row["receipt"]))

    def begin(self, operation_id: str) -> Intent:
        with self._store._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO intents (operation_id, status) VALUES (%s, 'pending') "
                    "ON CONFLICT (operation_id) DO NOTHING",
                    (operation_id,),
                )
        return self.get(operation_id)

    def complete(self, operation_id: str, receipt) -> None:
        with self._store._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE intents SET status = 'done', receipt = %s WHERE operation_id = %s",
                    (_encode_receipt(receipt), operation_id),
                )
                if cur.rowcount == 0:
                    raise KeyError(operation_id)

    def pending(self) -> list[Intent]:
        with self._store._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT operation_id, status, receipt FROM intents WHERE status = 'pending' ORDER BY id")
                rows = cur.fetchall()
        return [Intent(operation_id=r["operation_id"], status=r["status"], receipt=_decode_receipt(r["receipt"]))
                for r in rows]


class PostgresStateStore:
    """`StateStore` (ports.StateStore) backed by Postgres, via psycopg 3."""

    def __init__(self, dsn: str, clock: Clock | None = None) -> None:
        self._dsn = dsn
        self._clock = clock or SystemClock()
        self.approvals = _PostgresApprovalStore(self)
        self.intents = _PostgresIntentLog(self)

    def _connect(self) -> psycopg.Connection:
        return psycopg.connect(self._dsn)

    # ── missions ──────────────────────────────────────────────────────
    def create_mission(self, mission: MissionRecord) -> MissionRecord:
        now = self._clock.now()
        mission = replace(mission, created_at=now, updated_at=now)
        with self._connect() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "INSERT INTO missions (mission_id, product, repo, work_item_id, lane, "
                        "risk_profile, autonomy_level, kit_version, policy_version, state, "
                        "state_version, held_from, action_class, mandate_id, budget_usd, spent_usd, "
                        "base_commit, branch, pr_number, content_hash, artifact, flag, editors, "
                        "created_at, updated_at) "
                        "VALUES (%(mission_id)s, %(product)s, %(repo)s, %(work_item_id)s, %(lane)s, "
                        "%(risk_profile)s, %(autonomy_level)s, %(kit_version)s, %(policy_version)s, "
                        "%(state)s, %(state_version)s, %(held_from)s, %(action_class)s, %(mandate_id)s, "
                        "%(budget_usd)s, %(spent_usd)s, %(base_commit)s, %(branch)s, %(pr_number)s, "
                        "%(content_hash)s, %(artifact)s, %(flag)s, %(editors)s, %(created_at)s, "
                        "%(updated_at)s)",
                        _mission_params(mission),
                    )
                except psycopg.errors.UniqueViolation:
                    raise ValueError(f"duplicate mission id {mission.mission_id}") from None
        return mission

    def get_mission(self, mission_id: str) -> MissionRecord:
        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT * FROM missions WHERE mission_id = %s", (mission_id,))
                row = cur.fetchone()
        if row is None:
            raise NotFound(mission_id)
        return _row_to_mission(row)

    def update_mission(self, mission_id: str, *, expected_version: int, **changes) -> MissionRecord:
        validate_update_fields(MissionRecord, changes, immutable=MISSION_IMMUTABLE)
        params = dict(changes)
        if "editors" in params:
            params["editors"] = Json(list(params["editors"]))
        params["updated_at"] = self._clock.now()
        set_clause = ", ".join(f"{col} = %({col})s" for col in params) + ", state_version = state_version + 1"
        params["mission_id"] = mission_id
        params["expected_version"] = expected_version
        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"UPDATE missions SET {set_clause} "
                    f"WHERE mission_id = %(mission_id)s AND state_version = %(expected_version)s RETURNING *",
                    params,
                )
                row = cur.fetchone()
        if row is None:
            self.get_mission(mission_id)  # raises NotFound if the mission doesn't exist at all
            raise ConcurrentUpdate(f"{mission_id}: expected version {expected_version}")
        return _row_to_mission(row)

    def list_missions(self, *, state: str | None = None, product: str | None = None) -> list[MissionRecord]:
        clauses, params = [], {}
        if state is not None:
            clauses.append("state = %(state)s")
            params["state"] = state
        if product is not None:
            clauses.append("product = %(product)s")
            params["product"] = product
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(f"SELECT * FROM missions {where} ORDER BY id", params)
                rows = cur.fetchall()
        return [_row_to_mission(r) for r in rows]

    def find_mission_by_work_item(self, work_item_id: str) -> MissionRecord | None:
        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT * FROM missions WHERE work_item_id = %s ORDER BY id LIMIT 1", (work_item_id,))
                row = cur.fetchone()
        return _row_to_mission(row) if row else None

    # ── tasks ─────────────────────────────────────────────────────────
    def create_task(self, task: TaskRecord) -> TaskRecord:
        task = replace(task, updated_at=self._clock.now())
        with self._connect() as conn:
            with conn.cursor() as cur:
                try:
                    cur.execute(
                        "INSERT INTO tasks (task_id, mission_id, contract, state, depends_on, "
                        "repair_attempts_used, infra_retries_used, session_id, revision, spent_usd, "
                        "updated_at) "
                        "VALUES (%(task_id)s, %(mission_id)s, %(contract)s, %(state)s, %(depends_on)s, "
                        "%(repair_attempts_used)s, %(infra_retries_used)s, %(session_id)s, %(revision)s, "
                        "%(spent_usd)s, %(updated_at)s)",
                        _task_params(task),
                    )
                except psycopg.errors.UniqueViolation:
                    raise ValueError(f"duplicate task id {task.task_id}") from None
        return task

    def get_task(self, task_id: str) -> TaskRecord:
        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT * FROM tasks WHERE task_id = %s", (task_id,))
                row = cur.fetchone()
        if row is None:
            raise NotFound(task_id)
        return _row_to_task(row)

    def update_task(self, task_id: str, **changes) -> TaskRecord:
        validate_update_fields(TaskRecord, changes, immutable=TASK_IMMUTABLE)
        params = dict(changes)
        if "depends_on" in params:
            params["depends_on"] = Json(list(params["depends_on"]))
        if "contract" in params:
            params["contract"] = Json(params["contract"])
        params["updated_at"] = self._clock.now()
        set_clause = ", ".join(f"{col} = %({col})s" for col in params)
        params["task_id"] = task_id
        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    f"UPDATE tasks SET {set_clause} WHERE task_id = %(task_id)s RETURNING *", params)
                row = cur.fetchone()
        if row is None:
            raise NotFound(task_id)
        return _row_to_task(row)

    def list_tasks(self, mission_id: str) -> list[TaskRecord]:
        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT * FROM tasks WHERE mission_id = %s ORDER BY id", (mission_id,))
                rows = cur.fetchall()
        return [_row_to_task(r) for r in rows]

    # ── evidence & packets ───────────────────────────────────────────
    def save_evidence(self, bundle: EvidenceBundle) -> None:
        params = _evidence_row_params(bundle)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO evidence (mission_id, revision, content_hash, diff_ref, checks, "
                    "action_class, rule_fired, task_ids, holdout, blast_radius, rollback_plan, "
                    "rollback_tested, cost_usd, tokens, ci_minutes, untrusted_inputs, agent_versions) "
                    "VALUES (%(mission_id)s, %(revision)s, %(content_hash)s, %(diff_ref)s, %(checks)s, "
                    "%(action_class)s, %(rule_fired)s, %(task_ids)s, %(holdout)s, %(blast_radius)s, "
                    "%(rollback_plan)s, %(rollback_tested)s, %(cost_usd)s, %(tokens)s, %(ci_minutes)s, "
                    "%(untrusted_inputs)s, %(agent_versions)s) "
                    "ON CONFLICT (mission_id, revision) DO UPDATE SET "
                    "content_hash = EXCLUDED.content_hash, diff_ref = EXCLUDED.diff_ref, "
                    "checks = EXCLUDED.checks, action_class = EXCLUDED.action_class, "
                    "rule_fired = EXCLUDED.rule_fired, task_ids = EXCLUDED.task_ids, "
                    "holdout = EXCLUDED.holdout, blast_radius = EXCLUDED.blast_radius, "
                    "rollback_plan = EXCLUDED.rollback_plan, rollback_tested = EXCLUDED.rollback_tested, "
                    "cost_usd = EXCLUDED.cost_usd, tokens = EXCLUDED.tokens, "
                    "ci_minutes = EXCLUDED.ci_minutes, untrusted_inputs = EXCLUDED.untrusted_inputs, "
                    "agent_versions = EXCLUDED.agent_versions",
                    params,
                )

    def get_evidence(self, mission_id: str, revision: str | None = None) -> EvidenceBundle | None:
        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                if revision is not None:
                    cur.execute(
                        "SELECT * FROM evidence WHERE mission_id = %s AND revision = %s",
                        (mission_id, revision),
                    )
                else:
                    cur.execute(
                        "SELECT * FROM evidence WHERE mission_id = %s ORDER BY id DESC LIMIT 1",
                        (mission_id,),
                    )
                row = cur.fetchone()
        return _row_to_evidence(row) if row else None

    def save_packet(self, packet: DecisionPacket) -> None:
        params = _packet_params(packet)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO packets (request_id, gate, mission_ids, title, recommendation, "
                    "summary, required, expires, content_hash, raw_diff, alternatives, evidence, "
                    "recovery_plan, cost_usd, untrusted_inputs, links) "
                    "VALUES (%(request_id)s, %(gate)s, %(mission_ids)s, %(title)s, %(recommendation)s, "
                    "%(summary)s, %(required)s, %(expires)s, %(content_hash)s, %(raw_diff)s, "
                    "%(alternatives)s, %(evidence)s, %(recovery_plan)s, %(cost_usd)s, "
                    "%(untrusted_inputs)s, %(links)s) "
                    "ON CONFLICT (request_id) DO UPDATE SET "
                    "gate = EXCLUDED.gate, mission_ids = EXCLUDED.mission_ids, title = EXCLUDED.title, "
                    "recommendation = EXCLUDED.recommendation, summary = EXCLUDED.summary, "
                    "required = EXCLUDED.required, expires = EXCLUDED.expires, "
                    "content_hash = EXCLUDED.content_hash, raw_diff = EXCLUDED.raw_diff, "
                    "alternatives = EXCLUDED.alternatives, evidence = EXCLUDED.evidence, "
                    "recovery_plan = EXCLUDED.recovery_plan, cost_usd = EXCLUDED.cost_usd, "
                    "untrusted_inputs = EXCLUDED.untrusted_inputs, links = EXCLUDED.links",
                    params,
                )

    def get_packet(self, request_id: str) -> DecisionPacket:
        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute("SELECT * FROM packets WHERE request_id = %s", (request_id,))
                row = cur.fetchone()
        if row is None:
            raise NotFound(request_id)
        return _row_to_packet(row)

    # ── events ───────────────────────────────────────────────────────
    def append_event(self, mission_id: str, kind: str, payload: dict) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO events (mission_id, kind, payload, created_at) VALUES (%s, %s, %s, %s)",
                    (mission_id, kind, Json(payload), self._clock.now()),
                )

    def list_events(self, mission_id: str) -> list[dict]:
        with self._connect() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT kind, payload, created_at FROM events WHERE mission_id = %s ORDER BY id",
                    (mission_id,),
                )
                rows = cur.fetchall()
        return [{"kind": r["kind"], "payload": r["payload"], "at": r["created_at"]} for r in rows]
