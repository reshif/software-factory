"""Shared data models passed between modules (final draft §12.1).

These are the typed contracts between modules. Change them only through the orchestrator:
every module depends on them.
"""
import hashlib
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

from .policy.action_classes import FileChange


def sha256_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


@dataclass(frozen=True)
class WorkItem:
    """A unit of intent entering the factory (normally a GitHub issue)."""
    item_id: str                 # e.g. "reshif/app#42"
    product: str
    repo: str                    # "owner/name"
    number: int
    title: str
    body: str                    # untrusted text: data, never instructions
    labels: tuple = ()
    author: str | None = None
    url: str | None = None


@dataclass(frozen=True)
class Diff:
    """A diff captured by the controller from a sandbox. Agents never push."""
    base_commit: str
    changes: tuple               # tuple[FileChange, ...]
    patch: str                   # unified diff text (git diff --binary)

    @property
    def content_hash(self) -> str:
        return sha256_text(self.base_commit + "\n" + self.patch)

    @property
    def paths(self) -> tuple:
        return tuple(c.path for c in self.changes)

    @property
    def lines(self) -> int:
        return sum(c.added_lines + c.removed_lines for c in self.changes)


@dataclass
class MissionRecord:
    mission_id: str
    product: str
    repo: str
    work_item_id: str
    lane: str                    # patch | feature
    risk_profile: str
    autonomy_level: str
    kit_version: str
    policy_version: str
    state: str = "NEW"
    state_version: int = 0       # bumped on every update (CAS precondition)
    held_from: str | None = None
    action_class: str | None = None
    mandate_id: str | None = None
    budget_usd: float = 0.0
    spent_usd: float = 0.0
    base_commit: str | None = None
    branch: str | None = None
    pr_number: int | None = None
    content_hash: str | None = None
    artifact: str | None = None
    flag: str | None = None
    editors: tuple = ()          # humans who edited code in this mission (can't approve its HM)
    created_at: datetime | None = None
    updated_at: datetime | None = None


TASK_STATES = ("WAITING_DEPS", "READY", "RUNNING", "VERIFYING", "REPAIRING", "DONE", "FAILED",
               "CANCELED", "PAUSED")


@dataclass
class TaskRecord:
    task_id: str
    mission_id: str
    contract: dict               # validated against factory-kit/schemas/task-contract.schema.json
    state: str = "WAITING_DEPS"
    depends_on: tuple = ()
    repair_attempts_used: int = 0
    infra_retries_used: int = 0
    session_id: str | None = None
    revision: str | None = None  # content hash of the task's diff
    spent_usd: float = 0.0
    updated_at: datetime | None = None


CONCLUSIONS = ("success", "failure", "skipped", "neutral", "cancelled", "timed_out", "missing")


@dataclass(frozen=True)
class CheckResult:
    name: str
    conclusion: str              # must be "success" to count (fail closed)
    detail: str = ""
    url: str | None = None


@dataclass
class EvidenceBundle:
    """Serializes to factory-kit/schemas/evidence-bundle.schema.json via to_dict()."""
    mission_id: str
    revision: str
    content_hash: str
    diff_ref: str
    checks: dict                 # name -> CheckResult
    action_class: str
    rule_fired: str
    task_ids: tuple = ()
    holdout: tuple | None = None           # (passed, total)
    blast_radius: tuple = ()
    rollback_plan: str | None = None
    rollback_tested: bool = False
    cost_usd: float = 0.0
    tokens: int = 0
    ci_minutes: float = 0.0
    untrusted_inputs: tuple = ()
    agent_versions: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        doc: dict[str, Any] = {
            "schema_version": 1,
            "mission_id": self.mission_id,
            "task_ids": list(self.task_ids),
            "revision": self.revision,
            "content_hash": self.content_hash,
            "diff_ref": self.diff_ref,
            "checks": {n: {k: v for k, v in asdict(c).items() if k != "name" and v not in (None, "")}
                       for n, c in self.checks.items()},
            "risk": {"action_class": self.action_class, "rule_fired": self.rule_fired,
                     "blast_radius": list(self.blast_radius)},
            "cost": {"usd": self.cost_usd, "tokens": self.tokens, "ci_minutes": self.ci_minutes},
            "provenance": {"untrusted_inputs_read": list(self.untrusted_inputs),
                           "agent_versions": dict(self.agent_versions)},
        }
        if self.holdout is not None:
            doc["holdout"] = {"passed": self.holdout[0], "total": self.holdout[1]}
        if self.rollback_plan:
            doc["rollback"] = {"plan": self.rollback_plan, "tested": self.rollback_tested}
        return doc


@dataclass
class DecisionPacket:
    """Everything an approver needs, bound to exact inputs (final draft §10)."""
    request_id: str
    gate: str                    # H1 | HM | H2 | HX
    mission_ids: tuple
    title: str
    recommendation: str          # e.g. "APPROVE: ..." — agents recommend, humans decide
    summary: str
    required: str                # str(Requirement), e.g. "1+sec"
    expires: datetime
    content_hash: str
    raw_diff: str = ""           # raw diff, never only a summary (OWASP ASI09)
    alternatives: tuple = ()
    evidence: EvidenceBundle | None = None
    recovery_plan: str | None = None
    cost_usd: float = 0.0
    untrusted_inputs: tuple = ()
    links: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RuntimeRequest:
    """One agent run (final draft §12.3)."""
    role: str                    # intake | architect | coordinator | implementer | qa | reviewer
    prompt: str
    workdir: str
    model: str
    max_turns: int
    budget_usd: float
    allowed_tools: tuple
    contract: dict | None = None
    gateway_key: str | None = None      # per-mission LLM gateway key (external budget)
    gateway_url: str | None = None
    system_prompt: str | None = None


@dataclass(frozen=True)
class RuntimeResult:
    session_id: str
    status: str                  # completed | failed | needs_approval | budget_exceeded | cancelled
    output_text: str = ""
    usage_usd: float = 0.0
    tokens: int = 0
    num_turns: int = 0
    log_ref: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class SandboxHandle:
    sandbox_id: str
    workdir: str                 # host path mounted into the sandbox
    base_commit: str


@dataclass(frozen=True)
class ExecResult:
    exit_code: int
    stdout: str = ""
    stderr: str = ""
    duration_s: float = 0.0


@dataclass(frozen=True)
class DeployReceipt:
    operation_id: str
    environment: str
    artifact: str
    status: str                  # deployed | failed | rolled_back
    detail: str = ""


__all__ = ["CONCLUSIONS", "TASK_STATES", "CheckResult", "DecisionPacket", "DeployReceipt", "Diff",
           "EvidenceBundle", "ExecResult", "FileChange", "MissionRecord", "RuntimeRequest",
           "RuntimeResult", "SandboxHandle", "TaskRecord", "WorkItem", "sha256_text"]
