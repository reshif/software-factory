"""The mission lifecycle orchestrator (final draft §7, §14.1; build spec §3 B7).

`Factory` wires the Wave-1 ports (`factory.ports`) into the state machine and
gate table that already live in `factory.controller`/`factory.policy` (the
shared deterministic core every module depends on). It only imports adapter
*ports* and the shared controller/policy modules -- concrete adapters (the
real or fake GitHub client, runtime, sandbox, ...) are assembled once, in
`wiring.py`, and handed to `Factory` through its constructor.

Every mission update goes through `StateStore.update_mission` (a CAS on
`state_version`); every side effect (push, merge, deploy, revert) goes through
`controller.intents.execute_once` with a stable operation id, so a crash
between "intent recorded" and "receipt written" is reconciled rather than
repeated (final draft §10, walkthrough 7).

**v1 scope notes** (see `factory-controller/README.md` for the full list):
  - The coordinator agent is bypassed: tasks are created directly from the
    architect's `tasks[]` (build spec §3 B7 explicitly allows this).
  - The QA agent is not invoked automatically. The two required, wired
    independent-verification layers are the sandboxed check commands
    (`verification.local_checks`) and the reviewer agent
    (`verification.review`) -- the build spec's numbered lifecycle (§3 step 5)
    names exactly these two, not QA, for the automated loop.
  - Fan-out tasks are released up to `max_parallel_tasks` but executed one at
    a time in-process; the AND join-barrier semantics are the same as true
    concurrency would give, just not the wall-clock parallelism.
  - A production regression always escalates to HX (no automated diagnosis/
    fix path exists before the Phase-4 ops agent), matching final draft §7's
    "with no automated fix path -> AWAITING_HX".
  - Every `SandboxPort.create` is preceded by `RepoMirror.sync(repo)`
    (`_ensure_mirror`), which fails closed on a sync error. In `local` mode
    the demo's `FakeMirror`/harness path is a no-op over a repo it already
    created directly; in production `GitMirror` fetches into `repos_root`.
"""
from __future__ import annotations

import html
import logging
import os
import shlex
import shutil
import uuid
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

from .. import globs
from ..agent_output import AgentOutputError, parse_agent_output
from ..controller import mission_fsm
from ..controller.approval_budget import MINUTES_PER_UNIT, can_admit
from ..controller.approvals import ApprovalError, ApprovalRequest, IneligibleApprover, StaleApproval
from ..controller.coverage import CoverageResult, check_diff, check_request
from ..controller.gate_resolver import GateDecision, hx_requirement, release_requirement, resolve
from ..controller.intents import execute_once
from ..models import (CheckResult, Diff, EvidenceBundle, MissionRecord, RuntimeRequest, TaskRecord,
                      WorkItem, sha256_text)
from ..policy import Policy, Requirement
from ..policy.action_classes import Classification, FileChange, classify
from ..ports import (AgentRuntime, BudgetGateway, Clock, ConcurrentUpdate, DeployTarget, FlagProvider,
                     GitHubPort, HoldoutRunner, Notifier, NotFound, RepoMirror, SandboxPort, StateStore)
from ..github.client import MergeConflict
from ..release.revert import auto_revert
from ..telemetry import metrics as telemetry
from ..verification.checks import evaluate as evaluate_checks
from ..verification.evidence import build_evidence
from ..verification.local_checks import run_local_checks
from ..verification.review import run_review
from . import packets
from .agent_io import load_prompt, quote_untrusted
from .products import Product, ProductRegistry
from .tool_guard import ToolGuard

logger = logging.getLogger(__name__)

MODEL_TIER = {"intake": "triage", "architect": "planning", "implementer": "build", "reviewer": "planning"}
MAX_PARALLEL_TASKS_DEFAULT = 2
DISCOVERY_MAX_TURNS = 30
IMPLEMENT_MAX_TURNS = 60
REVIEW_MAX_TURNS = 20
DISCOVERY_BUDGET_USD = 3.0
PROD_ENVIRONMENT = "production"
STAGING_ENVIRONMENT = "staging"
# Controller scratch files (e.g. the patch applied during integration) live under this
# directory inside the sandbox workdir -- `SandboxPort.exec` has no stdin, so this is
# the best available way to hand the sandbox content without writing loose files an
# agent could collide with. It is always scrubbed before `capture_diff` (red-team #2
# item 5): nothing the controller writes here may ever appear in a captured diff.
CONTROLLER_TMP_DIRNAME = ".factory-controller-tmp"
PR_SUMMARY_MAX_CHARS = 2000
MAX_PACKET_DIFF_CHARS = 200_000
MAX_WEBHOOK_ATTEMPTS = 5
AWAITING_STATES = ("AWAITING_H1", "AWAITING_HM", "AWAITING_H2", "AWAITING_HX")


class MissionNotFound(NotFound):
    pass


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


class Factory:
    """Orchestrates one product cell's missions from intake to delivery."""

    def __init__(self, *, store: StateStore, github: GitHubPort, runtime: AgentRuntime,
                sandbox: SandboxPort, budget: BudgetGateway, notifier: Notifier,
                holdout: HoldoutRunner, deploy: DeployTarget, flags: FlagProvider,
                products: ProductRegistry, policy: Policy, clock: Clock, kit_dir: str | Path,
                inbox_base_url: str, evidence_dir: str, repos_root: str, mirror: RepoMirror,
                gateway_url: str | None = None, max_parallel_tasks: int = MAX_PARALLEL_TASKS_DEFAULT,
                state_encryption_key: str | None = None):
        self._store = store
        self._github = github
        self._runtime = runtime
        self._sandbox = sandbox
        self._budget = budget
        self._notifier = notifier
        self._holdout = holdout
        self._deploy = deploy
        self._flags = flags
        self._products = products
        self._policy = policy
        self._clock = clock
        self._kit_dir = Path(kit_dir)
        self._inbox_base_url = inbox_base_url.rstrip("/")
        self._evidence_dir = evidence_dir
        self._repos_root = repos_root
        self._mirror = mirror
        self._gateway_url = gateway_url
        self._max_parallel_tasks = max_parallel_tasks
        # Never store a raw gateway/discovery key in an event (red team #3 item
        # 6/M3): every key cached via `_remember_key` is Fernet-encrypted first.
        # `local` mode (and any caller that doesn't pass one) gets an ephemeral,
        # process-lifetime key -- there's nothing durable worth protecting past
        # this process's own in-memory fakes, and no operator to hand a real one
        # to; `production` mode's `Settings.validate` requires a real one, wired
        # through from `settings.state_encryption_key`.
        from cryptography.fernet import Fernet
        key_material = state_encryption_key.encode() if state_encryption_key else Fernet.generate_key()
        self._fernet = Fernet(key_material)

        # Process-local caches. Everything durable (mission/task state, approvals,
        # intents, evidence) lives in `store`; these only hold values that are
        # either cheap to recompute (a re-run implementer task) or re-derivable
        # from `store.list_events` after a restart (see `_remember`/`_recall`).
        self._gateway_keys: dict[str, str] = {}
        self._architect_plans: dict[str, dict] = {}
        self._task_diffs: dict[str, Diff] = {}
        self._work_items: dict[str, WorkItem] = {}

        # Webhook deliveries and approval decisions are durable (`store.enqueue_webhook`/
        # `claim_webhooks`/`ack_webhook`; `store.approvals`), not in-process queues
        # (red team #3 H1): any Factory instance sharing this store -- a `serve`
        # process and a `worker` process, say -- converges on the same outcome
        # regardless of which instance recorded the webhook or the decision.
        # This is the only process-local, best-effort bookkeeping left: a retry
        # counter for the webhook poison-message guard (resets on restart, which
        # just means a few extra retries, never fewer than the guard requires).
        self._webhook_attempts: dict[str, int] = {}

    # ── public accessors for the worker loop (`worker.py`) and the inbox/app ───
    @property
    def store(self) -> StateStore:
        return self._store

    @property
    def notifier(self) -> Notifier:
        return self._notifier

    @property
    def clock(self) -> Clock:
        return self._clock

    def product_for(self, mission: MissionRecord) -> Product:
        return self._product(mission)

    def transition(self, mission: MissionRecord, event: str, **changes) -> MissionRecord:
        return self._transition(mission, event, **changes)

    def remember(self, mission_id: str, kind: str, payload: dict) -> None:
        self._remember(mission_id, kind, payload)

    def recall(self, mission_id: str, kind: str) -> dict | None:
        return self._recall(mission_id, kind)

    # ── small helpers ────────────────────────────────────────────────────────────
    def _product(self, mission: MissionRecord) -> Product:
        return self._products.get(mission.product)

    def _remember(self, mission_id: str, kind: str, payload: dict) -> None:
        self._store.append_event(mission_id, f"_cache.{kind}", payload)

    def _recall(self, mission_id: str, kind: str) -> dict | None:
        for event in reversed(self._store.list_events(mission_id)):
            if event.get("kind") == f"_cache.{kind}":
                return event.get("payload", event)
        return None

    def _remember_key(self, mission_id: str, kind: str, key: str) -> None:
        """Like `_remember`, but for an LLM gateway budget key specifically: the
        raw key never touches `store.append_event` (and so never `list_events`),
        only its Fernet ciphertext (red team #3 item 6/M3)."""
        token = self._fernet.encrypt(key.encode()).decode()
        self._remember(mission_id, kind, {"key": token})

    def _recall_key(self, mission_id: str, kind: str) -> str | None:
        cached = self._recall(mission_id, kind)
        if not cached:
            return None
        try:
            return self._fernet.decrypt(cached["key"].encode()).decode()
        except Exception:  # noqa: BLE001 -- a key from a previous encryption key: treat as absent
            logger.warning("%s: could not decrypt cached %s (rotated state_encryption_key?)", mission_id, kind)
            return None

    def _gateway_key(self, mission: MissionRecord) -> str:
        """The mission's current gateway key -- minted fresh if there isn't one on
        record, or if the last-known one was explicitly revoked (red team #3 item
        5/M2: a mission that paused in HELD/BLOCKED and has since resumed must
        never let an agent run reuse the key that was revoked when it paused)."""
        key = self._gateway_keys.get(mission.mission_id)
        if key is None:
            key = self._recall_key(mission.mission_id, "gateway_key")
        revoked = self._recall_key(mission.mission_id, "gateway_key_revoked")
        if key is None or (revoked and revoked == key):
            key = self._budget.create_key(mission.mission_id, mission.budget_usd or DISCOVERY_BUDGET_USD)
            self._remember_key(mission.mission_id, "gateway_key", key)
        self._gateway_keys[mission.mission_id] = key
        return key

    def _discovery_key(self, mission: MissionRecord) -> str:
        """A small per-mission budget key for intake/architect (the discovery
        allowance, final draft §6.1): a real `AgentRuntime` needs SOME gateway
        key to run at all (it never sees a raw provider key, §13.1 #7), and
        discovery happens before the mission has its real admission budget.
        Revoked once discovery is no longer needed (`_revoke_discovery_key`).
        """
        cached = self._recall_key(mission.mission_id, "discovery_key")
        if cached:
            return cached
        key = self._budget.create_key(f"{mission.mission_id}-discovery", DISCOVERY_BUDGET_USD)
        self._remember_key(mission.mission_id, "discovery_key", key)
        return key

    def _revoke_discovery_key(self, mission_id: str) -> None:
        cached = self._recall_key(mission_id, "discovery_key")
        if not cached:
            return
        try:
            self._budget.revoke(cached)
        except Exception:  # noqa: BLE001 -- best-effort cleanup, never blocks the caller
            logger.warning("%s: could not revoke the discovery budget key", mission_id, exc_info=True)

    # States where nothing should ever be able to spend against this mission's
    # budget key again: it's either finished (DELIVERED) or paused indefinitely
    # (CANCELED/ARCHIVED/HELD/BLOCKED) -- red team #3 item 5/M2.
    _KEY_REVOKING_STATES = frozenset({"CANCELED", "ARCHIVED", "HELD", "BLOCKED", "DELIVERED"})

    def _revoke_mission_keys(self, mission_id: str) -> None:
        self._revoke_discovery_key(mission_id)
        key = self._gateway_keys.pop(mission_id, None)
        if key is None:
            key = self._recall_key(mission_id, "gateway_key")
        if key is None:
            return
        try:
            self._budget.revoke(key)
        except Exception:  # noqa: BLE001 -- best-effort cleanup, never blocks the transition
            logger.warning("%s: could not revoke the gateway key", mission_id, exc_info=True)
        # Marks this specific key as dead, so `_gateway_key` mints a fresh one
        # instead of reusing it if the mission later resumes.
        self._remember_key(mission_id, "gateway_key_revoked", key)

    def _event(self, mission_id: str, kind: str, payload: dict) -> None:
        self._store.append_event(mission_id, kind, payload)

    def _transition(self, mission: MissionRecord, event: str, **changes) -> MissionRecord:
        m = mission_fsm.Mission(mission.mission_id, state=mission.state, held_from=mission.held_from)
        new_state = m.fire(event)
        try:
            updated = self._store.update_mission(mission.mission_id, expected_version=mission.state_version,
                                                  state=new_state, held_from=m.held_from, **changes)
        except ConcurrentUpdate:
            # Someone else updated the mission concurrently; re-read and let the
            # caller retry against fresh state rather than silently overwrite it.
            raise
        self._event(mission.mission_id, "mission_transition", {"from": mission.state, "event": event,
                                                                "to": new_state})
        if new_state in self._KEY_REVOKING_STATES:
            self._revoke_mission_keys(mission.mission_id)
        return updated

    def _model_for(self, product: Product, role: str) -> str:
        return product.models[MODEL_TIER[role]]

    def _run_agent(self, *, role: str, product: Product, workdir: str, prompt: str, gateway_key: str | None,
                  budget_usd: float, max_turns: int, mission_id: str | None = None,
                  on_tool_approval=None, contract: dict | None = None):
        agent_prompt = load_prompt(self._kit_dir, role)
        request = RuntimeRequest(
            role=role, prompt=prompt, workdir=workdir, model=self._model_for(product, role),
            max_turns=max_turns, budget_usd=budget_usd, allowed_tools=(), contract=contract,
            gateway_key=gateway_key, gateway_url=self._gateway_url, system_prompt=agent_prompt.system_prompt)
        result = self._runtime.run(request, on_tool_approval=on_tool_approval)
        self._record_agent_spend(mission_id, gateway_key, result.usage_usd)
        return result

    def _record_agent_spend(self, mission_id: str | None, gateway_key: str | None, usage_usd: float) -> None:
        """`mission.spent_usd` accumulates real gateway spend from every agent run,
        including `resume` calls (red team #3 item 12) -- packets report the
        mission's actual cost, not a permanently-zero default."""
        if not mission_id or not usage_usd:
            return
        self._event(mission_id, telemetry.COST_SPENT, {"usd": usage_usd})
        try:
            mission = self._mission(mission_id)
            self._store.update_mission(mission_id, expected_version=mission.state_version,
                                       spent_usd=mission.spent_usd + usage_usd)
        except (ConcurrentUpdate, NotFound):
            logger.warning("%s: could not record $%.4f of agent spend (concurrent update)", mission_id, usage_usd)
        add_spend = getattr(self._budget, "add_spend", None)
        if gateway_key and add_spend:
            try:
                add_spend(gateway_key, usage_usd)
            except Exception as exc:  # noqa: BLE001 -- surfaced by the caller's budget check
                logger.info("%s: gateway reports budget exceeded: %s", mission_id, exc)
                self._budget_exhausted(mission_id, reason=f"LLM gateway budget exhausted: {exc}")

    def _budget_exhausted(self, mission_id: str, *, reason: str) -> None:
        """Fire the FSM's global `budget_exhausted` event (red team #3 item 11):
        WORK_STATES -> AWAITING_HX. Best-effort -- a mission already paused,
        awaiting a human, or terminal has nothing to escalate."""
        try:
            mission = self._mission(mission_id)
            if mission.state not in mission_fsm.WORK_STATES:
                return
            mission = self._transition(mission, "budget_exhausted")
            product = self._product(mission)
            self._request_hx(mission, product, reason=reason)
        except Exception:  # noqa: BLE001 -- never let this crash the agent-run path
            logger.exception("%s: could not escalate the budget-exhausted mission to HX", mission_id)

    def _ensure_mirror(self, repo: str) -> None:
        """Sync `repo`'s local mirror before every `SandboxPort.create` (`RepoMirror.sync`,
        final draft §12.2 M8). Fails closed: a sync error must stop the caller, never
        fall back to a possibly-stale or missing mirror."""
        try:
            self._mirror.sync(repo)
        except Exception as exc:  # noqa: BLE001 -- fail closed
            raise RuntimeError(f"could not sync the git mirror for {repo!r}: {exc}") from exc

    def _issue_ref(self, work_item_id: str) -> tuple:
        repo, _, number = work_item_id.rpartition("#")
        return repo, int(number)

    def _scrub_controller_artifacts(self, workdir: str) -> None:
        """Remove the controller's own scratch directory before capturing a diff."""
        shutil.rmtree(os.path.join(workdir, CONTROLLER_TMP_DIRNAME), ignore_errors=True)

    def _capture_diff_clean(self, handle) -> Diff:
        """`SandboxPort.capture_diff`, after scrubbing controller-written scratch
        content (red-team #2 item 4/5): what gets pushed is only what an agent
        actually changed, never a check-run artifact or an integration patch file.
        """
        self._scrub_controller_artifacts(handle.workdir)
        return self._sandbox.capture_diff(handle)

    def _reject_out_of_scope_changes(self, diff: Diff, *, owned_paths, workdir: str) -> tuple:
        """Every changed path must be inside `owned_paths`, and none may be a symlink
        (red-team #2 item 4). Returns a tuple of human-readable problem strings,
        empty if the diff is clean."""
        problems = []
        out_of_scope = [c.path for c in diff.changes if not globs.match_any(c.path, owned_paths)]
        if out_of_scope:
            problems.append(f"changed paths outside owned_paths {list(owned_paths)}: {out_of_scope}")
        symlinks = [c.path for c in diff.changes
                   if os.path.islink(os.path.join(workdir, c.path))]
        if symlinks:
            problems.append(f"symlinks are not allowed: {symlinks}")
        return tuple(problems)

    # ── 1-2: intake + discovery (final draft §7 steps 1-2) ─────────────────────
    def start_mission(self, work_item: WorkItem) -> MissionRecord:
        """Idempotent: one mission per work item id."""
        existing = self._store.find_mission_by_work_item(work_item.item_id)
        if existing is not None:
            return existing
        product = self._products.for_repo(work_item.repo)
        self._work_items[work_item.item_id] = work_item

        mission = MissionRecord(
            mission_id=_new_id("MIS"), product=product.name, repo=work_item.repo,
            work_item_id=work_item.item_id, lane="patch", risk_profile=product.risk_profile,
            autonomy_level=product.autonomy_level, kit_version=product.kit_version,
            policy_version=product.policy_version, base_commit=self._github.head_commit(work_item.repo),
            created_at=self._clock.now(), updated_at=self._clock.now())
        mission = self._store.create_mission(mission)
        # Durable, not just process-local: the regulated-profile "requester can't
        # approve their own request" rule (final draft §6.3) must still hold after a
        # restart (red-team #2 item 9), so this can't live only in `self._work_items`.
        self._remember(mission.mission_id, "requester", {"login": work_item.author or "factory-bot"})

        suggested_class, lane, summary, risk_notes = self._run_intake(mission, product, work_item)
        mandate = product.mandate_for(labels=work_item.labels)
        coverage = CoverageResult(False, ("no standing mandate for this product",))
        if mandate is not None:
            coverage = check_request(mandate, labels=work_item.labels, suggested_class=suggested_class,
                                     today=self._clock.now().date())

        mission = self._store.update_mission(mission.mission_id, expected_version=mission.state_version,
                                             lane=lane)
        if coverage.covered:
            mission = self._transition(mission, "covered", mandate_id=mandate.mandate_id,
                                       action_class=suggested_class)
            self._admit(mission, product, tasks=[self._patch_task(mission, product, mandate, work_item,
                                                                   suggested_class)])
        else:
            mission = self._transition(mission, "not_covered")
            logger.info("%s: not covered by a standing mandate (%s); entering discovery",
                       mission.mission_id, "; ".join(coverage.reasons))
            self._run_discovery(mission, product, work_item)
        return self._store.get_mission(mission.mission_id)

    def _run_intake(self, mission: MissionRecord, product: Product, work_item: WorkItem):
        handle = None
        try:
            self._ensure_mirror(work_item.repo)
            handle = self._sandbox.create(work_item.repo, mission.base_commit)
            prompt = (f"Work item {work_item.item_id}: {work_item.title}\n\n"
                     f"{quote_untrusted('issue body', work_item.body)}\n\nLabels: {list(work_item.labels)}")
            guard = ToolGuard(workdir=handle.workdir, owned_paths=("**",),
                              forbidden_globs=self._policy.floor.forbidden_paths + product.forbidden_paths,
                              protected_globs=self._policy.floor.protected_paths + product.protected_paths)
            result = self._run_agent(role="intake", product=product, workdir=handle.workdir, prompt=prompt,
                                     gateway_key=self._discovery_key(mission), budget_usd=DISCOVERY_BUDGET_USD,
                                     max_turns=DISCOVERY_MAX_TURNS, mission_id=mission.mission_id,
                                     on_tool_approval=guard)
            output = parse_agent_output("intake", result.output_text, kit_dir=self._kit_dir)
            return output["suggested_class"], output["lane"], output["summary"], output["risk_notes"]
        except (AgentOutputError, Exception) as exc:  # noqa: BLE001 -- fail closed to the stricter path
            logger.warning("%s: intake failed (%s); falling back to feature/AC4 (fail closed)",
                          mission.mission_id, exc)
            return "AC4", "feature", f"intake failed: {exc}", ["intake output invalid or unavailable"]
        finally:
            if handle is not None:
                self._sandbox.destroy(handle)

    def _patch_task(self, mission: MissionRecord, product: Product, mandate, work_item: WorkItem,
                    action_class: str) -> dict:
        checks = [name for name in product.verification_required]
        return {"task_id": "T-1", "objective": work_item.title or f"Patch for {work_item.item_id}",
                "owned_paths": list(mandate.paths), "action_class": action_class,
                "acceptance_checks": checks, "depends_on": []}

    def _run_discovery(self, mission: MissionRecord, product: Product, work_item: WorkItem) -> None:
        handle = None
        try:
            self._ensure_mirror(work_item.repo)
            handle = self._sandbox.create(work_item.repo, mission.base_commit)
            prompt = (f"Work item {work_item.item_id}: {work_item.title}\n\n"
                     f"{quote_untrusted('issue body', work_item.body)}\n\n"
                     f"Product: {product.name} (risk profile {product.risk_profile}).")
            guard = ToolGuard(workdir=handle.workdir, owned_paths=(),
                              forbidden_globs=self._policy.floor.forbidden_paths + product.forbidden_paths,
                              protected_globs=self._policy.floor.protected_paths + product.protected_paths)
            result = self._run_agent(role="architect", product=product, workdir=handle.workdir, prompt=prompt,
                                     gateway_key=self._discovery_key(mission), budget_usd=DISCOVERY_BUDGET_USD,
                                     max_turns=DISCOVERY_MAX_TURNS, mission_id=mission.mission_id,
                                     on_tool_approval=guard)
            output = parse_agent_output("architect", result.output_text, kit_dir=self._kit_dir)
        except (AgentOutputError, Exception) as exc:  # noqa: BLE001 -- fail closed, never advance
            logger.warning("%s: architect output invalid (%s); escalating to HX", mission.mission_id, exc)
            self._notifier.info(f"{mission.mission_id}: discovery failed ({exc}); needs manual attention")
            # Never leave the mission stuck in DISCOVERING (red team #3 item 13):
            # an invalid architect output or a runtime error during discovery is
            # exactly the "no safe automated next step" case `_boundary` handles.
            self._boundary(self._mission(mission.mission_id), reason=f"discovery failed: {exc}")
            return
        finally:
            if handle is not None:
                self._sandbox.destroy(handle)

        self._architect_plans[mission.mission_id] = output
        self._remember(mission.mission_id, "architect_plan", output)
        task_classes = [t["action_class"] for t in output["tasks"]] or ["AC4"]
        worst_class = max(task_classes, key=lambda c: int(c[2]))
        gate = resolve(self._policy, action_class=worst_class, risk_profile=product.risk_profile,
                       autonomy_level=product.autonomy_level, overrides=product.gate_overrides)
        content_hash = sha256_text(f"{mission.mission_id}:{output['summary']}:{mission.state_version}")
        mission = self._store.update_mission(mission.mission_id, expected_version=mission.state_version,
                                             action_class=worst_class, content_hash=content_hash)
        mission = self._transition(mission, "packet_ready")
        self._request_approval(mission, product, gate="H1", required=gate.h1, title=f"H1: {output['summary']}",
                               recommendation=output["recommendation"], summary=output["summary"],
                               content_hash=content_hash, alternatives=tuple(output["alternatives"]),
                               recovery_plan=output["recovery_plan"], cost_usd=output["estimate_usd"],
                               untrusted_inputs=(f"issue body of {work_item.item_id}",))

    # ── approval requests + decisions (final draft §10, build spec §3 step 3) ──
    def _request_approval(self, mission: MissionRecord, product: Product, *, gate: str, required: Requirement,
                          title: str, recommendation: str, summary: str, content_hash: str,
                          raw_diff: str = "", alternatives: tuple = (), evidence: EvidenceBundle | None = None,
                          recovery_plan: str | None = None, cost_usd: float = 0.0,
                          untrusted_inputs: tuple = (), extra_mission_ids: tuple = ()) -> None:
        if not required.needs_human:
            raise ValueError(f"{gate} requirement {required} does not need a human approval request")
        request_id = packets.new_request_id(gate)
        timeout, _ = mission_fsm.TIMEOUTS[f"AWAITING_{gate}"]
        expires = self._clock.now() + timeout
        # Every request gets ITS OWN eligible approver -> roles map, from exactly
        # this product's factory.yaml for exactly this gate (red team #3 H2). This is
        # never a global union across products: `check_decision` rejects anyone not
        # in it and uses these roles regardless of what a caller claims.
        eligible = product.eligible_for(gate)
        request = ApprovalRequest(
            request_id=request_id, gate=gate, mission_ids=(mission.mission_id, *extra_mission_ids),
            operation_id=f"{gate}:{mission.mission_id}", artifact=mission.artifact, content_hash=content_hash,
            policy_version=mission.policy_version, state_version=mission.state_version, required=required,
            risk_profile=product.risk_profile, requester=self._requester(mission),
            expires=expires, editors=frozenset(mission.editors), eligible=eligible)
        self._store.approvals.add(request)
        if not raw_diff and evidence is not None:
            raw_diff = self._packet_diff(evidence)
        packet = packets.build_packet(request_id=request_id, gate=gate, mission=mission, title=title,
                                      recommendation=recommendation, summary=summary, required=str(required),
                                      expires=expires, content_hash=content_hash, raw_diff=raw_diff,
                                      alternatives=alternatives, evidence=evidence, recovery_plan=recovery_plan,
                                      cost_usd=cost_usd, untrusted_inputs=untrusted_inputs,
                                      extra_mission_ids=extra_mission_ids,
                                      # Forward-compatible hook for the notifier to scope its roster to THIS
                                      # request's eligible approvers only (red team #3 H2) -- `Notifier`/
                                      # `SlackNotifier` (inbox module) don't read this yet; see the final
                                      # report's "Requests to orchestrator" for routing this to that owner.
                                      links={"eligible_approvers": sorted(eligible)})
        self._store.save_packet(packet)
        # Durable pointer from mission -> its current request, so `process_approvals`
        # can find and react to a revise/cancel/defer decision even after
        # `ApprovalStore.decide` has already voided the request out of `list_open()`
        # (red team #3 H1; see `_process_terminal_decision`).
        self._remember(mission.mission_id, "open_request", {"request_id": request_id, "gate": gate})
        self._event(mission.mission_id, telemetry.APPROVAL_REQUESTED, {"gate": gate, "request_id": request_id})
        inbox_url = self._inbox_base_url
        self._notifier.decision_requested(packet, inbox_url=inbox_url)

    def _packet_diff(self, evidence: EvidenceBundle | None) -> str:
        """The raw diff for an HM/H2 packet (final draft §10 "the raw diff and a
        preview"), read from `evidence.diff_ref` and capped so a huge diff can't
        blow up the packet page -- a note replaces the tail past the cap."""
        if evidence is None or not evidence.diff_ref:
            return ""
        try:
            with open(evidence.diff_ref) as f:
                text = f.read(MAX_PACKET_DIFF_CHARS + 1)
        except OSError:
            logger.warning("could not read diff_ref %r for a packet", evidence.diff_ref)
            return ""
        if len(text) > MAX_PACKET_DIFF_CHARS:
            return text[:MAX_PACKET_DIFF_CHARS] + f"\n... [truncated; full diff at {evidence.diff_ref}]"
        return text

    def _requester(self, mission: MissionRecord) -> str:
        cached = self._recall(mission.mission_id, "requester")
        if cached:
            return cached["login"]
        item = self._work_items.get(mission.work_item_id)
        return (item.author if item and item.author else "factory-bot")

    def decide(self, request_id: str, approver: str, roles, decision: str, content_hash: str) -> ApprovalRequest:
        """The single entry point the inbox and GitHub review webhooks call (build spec §3 step 3).

        This ONLY records the decision in the approval store (red team #3 H1) --
        no queueing, no side effect, not even a cheap FSM transition. `process_approvals`
        (worker-driven, on every Factory instance sharing this store) is what acts on
        it, by scanning the store rather than draining an in-process queue: that's
        what lets a decision recorded by a `serve` process be carried out by a
        separate `worker` process, and vice versa.
        """
        request = self._store.approvals.decide(request_id, approver=approver, roles=roles, decision=decision,
                                               content_hash=content_hash, now=self._clock.now())
        for mission_id in request.mission_ids:
            self._event(mission_id, telemetry.APPROVAL_DECIDED,
                       {"request_id": request_id, "decision": decision, "approver": approver})
        return request

    def process_approvals(self) -> None:
        """Scan the shared approval store for work and act on it (red team #3 H1).

        Quorum-met requests are still `list_open()` (approve never voids), so a
        plain scan finds them. `revise`/`cancel` VOID the request immediately
        (`ApprovalStore.decide`), so it drops out of `list_open()` before any
        instance's `process_approvals` gets a chance to act on it -- that side
        effect is instead driven by the mission side: every AWAITING_* mission's
        current request id is durably recorded (`_request_approval`'s "open_request"
        cache event), so this looks each one up directly by id (works regardless of
        status) and reacts to its LAST decision, exactly once (a durable
        "processed_decision" marker prevents reprocessing after every scan).
        """
        for request in self._store.approvals.list_open():
            if request.quorum_met():
                try:
                    self._on_quorum(request)
                except ApprovalError as exc:
                    # A concurrent consume, an expiry, or a stale state/content/policy
                    # hash: never the follow-up action's own fault, so nothing to
                    # escalate to HX for -- just log it and let the scan continue.
                    logger.info("%s: consuming quorum-met request failed: %s", request.request_id, exc)
                except Exception:  # noqa: BLE001 -- one bad request must not wedge the scan
                    logger.exception("%s: processing a quorum-met request failed", request.request_id)
        for state in AWAITING_STATES:
            for mission in self._store.list_missions(state=state):
                self._process_terminal_decision(mission)

    def _process_terminal_decision(self, mission: MissionRecord) -> None:
        marker = self._recall(mission.mission_id, "open_request")
        if not marker:
            return
        try:
            request = self._store.approvals.get(marker["request_id"])
        except (KeyError, NotFound):
            return
        if not request.decisions:
            return
        last = request.decisions[-1]
        if last.decision == "approve":
            return  # handled by the quorum-met scan (or quorum not met yet)
        already = self._recall(mission.mission_id, "processed_decision")
        if already and already.get("request_id") == request.request_id and already.get("at") == last.at.isoformat():
            return
        try:
            if last.decision == "revise":
                self._on_revise(request)
            elif last.decision == "cancel":
                self._on_cancel(request)
            elif last.decision == "defer":
                self._on_defer(request)
        except ApprovalError as exc:
            logger.info("processing %s (%s) did not apply: %s", request.request_id, last.decision, exc)
        except Exception:  # noqa: BLE001 -- one bad request must not wedge the worker loop
            logger.exception("processing decision %s (%s) failed", request.request_id, last.decision)
        self._remember(mission.mission_id, "processed_decision",
                       {"request_id": request.request_id, "at": last.at.isoformat()})

    def _mission(self, mission_id: str) -> MissionRecord:
        return self._store.get_mission(mission_id)

    def _on_revise(self, request: ApprovalRequest) -> None:
        for mission_id in request.mission_ids:
            mission = self._mission(mission_id)
            if request.gate == "H1":
                mission = self._transition(mission, "revise")
                product = self._product(mission)
                work_item = self._work_items.get(mission.work_item_id)
                if work_item is None:
                    repo, number = self._issue_ref(mission.work_item_id)
                    work_item = replace(self._github.get_issue(repo, number), product=product.name)
                self._run_discovery(mission, product, work_item)
            elif request.gate in ("HM", "H2"):
                # "Changes requested" must not just re-integrate the identical diff
                # (red team #3 item 10): a real repair task forces the implementer to
                # actually run again. Neither ApprovalStore.Decision nor GitHub's
                # PullRequestReview carries free-text review comments today, so the
                # task objective can only say who asked and at which gate -- see the
                # final report's "Requests to orchestrator" for plumbing the actual
                # comment text through.
                mission = self._transition(mission, "changes_requested")
                product = self._product(mission)
                self._create_repair_task(mission, product,
                                         reason=f"changes requested at {request.gate}")
            elif request.gate == "HX":
                logger.info("%s: HX revise recorded; awaiting a fresh decision", mission_id)

    def _on_cancel(self, request: ApprovalRequest) -> None:
        for mission_id in request.mission_ids:
            mission = self._mission(mission_id)
            if request.gate == "H1":
                self._transition(mission, "decline")
                self._revoke_discovery_key(mission_id)
            elif request.gate == "HX":
                self._transition(mission, "declined")
            else:
                logger.info("%s: %s cancel recorded (no dedicated transition)", mission_id, request.gate)

    def _on_defer(self, request: ApprovalRequest) -> None:
        """defer -> HELD (red team #3 item 10); HX specifically -> BLOCKED, the FSM's
        own "deferred" event. H1/HM have no dedicated defer transition in the FSM
        (only H2 and HX do) -- for those the request simply stays open for a later
        decision or a timeout; deferring isn't a distinct state for them."""
        for mission_id in request.mission_ids:
            mission = self._mission(mission_id)
            if request.gate == "H2" and mission.state == "AWAITING_H2":
                self._transition(mission, "defer")
            elif request.gate == "HX" and mission.state == "AWAITING_HX":
                self._transition(mission, "deferred")
            else:
                logger.info("%s: %s defer recorded (no dedicated transition for this gate)",
                           mission_id, request.gate)

    def _on_quorum(self, request: ApprovalRequest) -> None:
        mission = self._mission(request.mission_ids[0])
        # Consume right before the side effect (final draft §10): this is the atomic
        # CAS + fencing-token step. If it raises, the approval was never spent --
        # nothing to recover, the caller (process_approvals) just logs it.
        fencing_token = self._store.approvals.consume(
            request.request_id, executor="factory-controller", now=self._clock.now(),
            state_version=mission.state_version, content_hash=request.content_hash,
            policy_version=mission.policy_version)
        # From here the approval IS spent. If the follow-up action itself fails
        # (an agent run, a GitHub/deploy call), the mission must not be left stuck
        # in its AWAITING_* state with no way forward -- land it in AWAITING_HX
        # instead, with the failure reason as evidence for a human to resolve.
        try:
            if request.gate == "H1":
                self._on_h1_approved(mission)
            elif request.gate == "HM":
                self._on_hm_approved(mission, fencing_token)
            elif request.gate == "H2":
                self._on_h2_approved(mission, fencing_token)
            elif request.gate == "HX":
                self._on_hx_approved(mission)
        except Exception as exc:  # noqa: BLE001 -- fail to a defined, recoverable state
            logger.exception("%s: %s approved but the follow-up action failed; escalating to HX",
                            mission.mission_id, request.gate)
            fresh = self._mission(mission.mission_id)
            if fresh.state not in mission_fsm.NO_WORKER:
                try:
                    self._boundary(fresh, reason=f"{request.gate} approved but the action failed: {exc}")
                except Exception:  # noqa: BLE001 -- last resort: at least don't crash the worker loop
                    logger.exception("%s: escalating to HX after a failed %s also failed",
                                    mission.mission_id, request.gate)

    # ── 4: admission (final draft §7 step "Admission") ──────────────────────────
    def _on_h1_approved(self, mission: MissionRecord) -> None:
        mission = self._transition(mission, "approved")
        product = self._product(mission)
        plan = self._architect_plans.get(mission.mission_id) or self._recall(mission.mission_id, "architect_plan")
        tasks = plan["tasks"] if plan else []
        self._admit(mission, product, tasks=tasks)

    def _task_contract_schema(self) -> dict:
        import json as _json
        with open(self._kit_dir / "schemas" / "task-contract.schema.json") as f:
            return _json.load(f)

    def _build_task_contract(self, mission: MissionRecord, product: Product, raw: dict, *, run_id: str) -> dict:
        """Wrap the architect's (or the patch lane's) raw per-task dict into a full,
        schema-valid task contract (build spec §3 step 4, final draft §12.1)."""
        lane = mission.lane
        contract = {
            "schema_version": 1,
            "ids": {"mission": mission.mission_id, "task": raw["task_id"], "run": run_id,
                    "depends_on": list(raw.get("depends_on", ()))},
            "objective": raw["objective"],
            "base_commit": mission.base_commit,
            "owned_paths": list(raw["owned_paths"]),
            "action_class": raw["action_class"],
            "tools": ["read", "edit", "run_tests"],
            "acceptance_checks": list(raw["acceptance_checks"]),
            "limits": {"repair_attempts": product.repair_attempts.get(lane, 2),
                      "infra_retries": product.infra_retries, "minutes": 45,
                      "usd": product.budgets[f"{lane}_task_usd"]},
        }
        import jsonschema
        jsonschema.validate(instance=contract, schema=self._task_contract_schema())
        return contract

    def _committed_minutes(self, product: Product, *, excluding: str) -> float:
        """Approval-reviewer minutes committed by every non-terminal mission for
        `product`, for `approval_budget.can_admit` (final draft §6.5's WIP limit)."""
        total = 0.0
        for m in self._store.list_missions(product=product.name):
            if m.mission_id != excluding and m.state not in mission_fsm.TERMINAL:
                total += MINUTES_PER_UNIT.get(m.lane, 0.0)
        return total

    def release_admitted_missions(self) -> None:
        """Fire `capacity_ok` for any ADMITTED mission once approval capacity AND
        the product's monthly budget both allow it. Called by the worker loop; a
        mission either was blocked for simply stays in ADMITTED (the queue)
        until this releases it (red team #3 item 9)."""
        for mission in self._store.list_missions(state="ADMITTED"):
            product = self._product(mission)
            if self._capacity_ok(mission, product):
                self._transition(mission, "capacity_ok")

    def _spent_this_month(self, product: Product) -> float:
        """Sum of `cost_spent` events, across every mission of `product`, whose
        timestamp falls in the current calendar month (red team #3 item 9's
        `monthly_usd` cap uses spend, not a separate counter, so it never drifts
        from what `_record_agent_spend` actually recorded)."""
        now = self._clock.now()
        total = 0.0
        for mission in self._store.list_missions(product=product.name):
            for event in self._store.list_events(mission.mission_id):
                if event.get("kind") != telemetry.COST_SPENT:
                    continue
                at = event.get("at")
                if at is not None and at.year == now.year and at.month == now.month:
                    total += event.get("payload", {}).get("usd", 0.0)
        return total

    def _capacity_ok(self, mission: MissionRecord, product: Product) -> bool:
        """Both halves of admission capacity (red team #3 item 9): reviewer
        WIP (`approval_budget.can_admit`) AND the product's `monthly_usd` cost
        cap. Either being full leaves the mission queued in ADMITTED."""
        committed = self._committed_minutes(product, excluding=mission.mission_id)
        if not can_admit(mission.lane, committed_minutes=committed,
                        reviewer_hours_per_week=product.budgets["reviewer_hours_per_week"]):
            return False
        monthly_cap = product.budgets["monthly_usd"]
        projected = self._spent_this_month(product) + (mission.budget_usd or 0.0)
        if projected > monthly_cap:
            logger.info("%s: staying ADMITTED, projected monthly spend $%.2f would exceed the $%.2f cap",
                       mission.mission_id, projected, monthly_cap)
            return False
        return True

    def _admit(self, mission: MissionRecord, product: Product, *, tasks: list) -> None:
        lane = mission.lane
        budget_usd = product.budgets[f"{lane}_mission_usd"]
        key = self._budget.create_key(mission.mission_id, budget_usd)
        self._gateway_keys[mission.mission_id] = key
        self._remember_key(mission.mission_id, "gateway_key", key)
        self._revoke_discovery_key(mission.mission_id)

        for i, raw in enumerate(tasks, start=1):
            raw = {**raw, "task_id": raw.get("task_id", f"T-{i}")}
            contract = self._build_task_contract(mission, product, raw, run_id=f"R-{i}")
            task_id = f"{mission.mission_id}:{raw['task_id']}"
            record = TaskRecord(task_id=task_id, mission_id=mission.mission_id, contract=contract,
                                state="READY" if not raw.get("depends_on") else "WAITING_DEPS",
                                depends_on=tuple(raw.get("depends_on", ())))
            self._store.create_task(record)
        mission = self._store.update_mission(mission.mission_id, expected_version=mission.state_version,
                                             budget_usd=budget_usd)
        # Otherwise it stays ADMITTED (queued); `release_admitted_missions` (the
        # worker's periodic tick) fires `capacity_ok` once capacity frees up.
        if self._capacity_ok(mission, product):
            self._transition(mission, "capacity_ok")

    # ── 5: tasks (final draft §7 step "Agents work in sandboxes", §9.1, §14.2) ──
    def run_ready_tasks(self, mission_id: str | None = None) -> None:
        """Release and run every READY task, up to `max_parallel_tasks` at a time.

        Called by the worker loop; the demo also calls it directly to drive a
        scenario forward a step. Tasks are executed sequentially in-process --
        the AND join-barrier semantics (§9.1) are the same as true concurrency
        would give.
        """
        self._check_mandate_expiry()
        missions = ([self._mission(mission_id)] if mission_id
                   else self._store.list_missions(state="ACTIVE"))
        for mission in missions:
            if mission.state != "ACTIVE":
                continue
            tasks = self._store.list_tasks(mission.mission_id)
            self._release_waiting(tasks)
            running = 0
            for task in self._store.list_tasks(mission.mission_id):
                if task.state == "READY" and running < self._max_parallel_tasks:
                    self._run_task(mission, task)
                    running += 1
            self._maybe_integrate(self._mission(mission.mission_id))

    def _check_mandate_expiry(self) -> None:
        """Fire `mandate_expired` (the FSM's global event, WORK_STATES ->
        AWAITING_HX) for any in-progress mission whose standing mandate's expiry
        date has passed since it was admitted (red team #3 item 11: this is the
        MID-RUN check -- `check_request`/`check_diff` already guard admission and
        each new diff, but a mandate can still lapse while a mission is sitting
        in ADMITTED/ACTIVE/etc. waiting on something else)."""
        today = self._clock.now().date()
        for state in mission_fsm.WORK_STATES:
            for mission in self._store.list_missions(state=state):
                if not mission.mandate_id:
                    continue
                product = self._product(mission)
                mandate = next((m for m in product.standing_mandates if m.mandate_id == mission.mandate_id), None)
                if mandate is None or today <= mandate.expires:
                    continue
                try:
                    mission = self._transition(mission, "mandate_expired")
                    self._request_hx(mission, product,
                                     reason=f"standing mandate {mandate.mandate_id} expired {mandate.expires}")
                except (mission_fsm.InvalidTransition, ConcurrentUpdate) as exc:
                    logger.warning("%s: mandate_expired did not apply: %s", mission.mission_id, exc)

    def _release_waiting(self, tasks: list) -> None:
        done_ids = {t.task_id.rsplit(":", 1)[-1] for t in tasks if t.state == "DONE"}
        for task in tasks:
            if task.state == "WAITING_DEPS" and set(task.depends_on) <= done_ids:
                self._store.update_task(task.task_id, state="READY")

    def _run_task(self, mission: MissionRecord, task: TaskRecord) -> None:
        product = self._product(mission)
        contract = task.contract
        lane = mission.lane
        max_repairs = product.repair_attempts.get(lane, 2)
        self._store.update_task(task.task_id, state="RUNNING")

        try:
            self._ensure_mirror(mission.repo)
            handle = self._sandbox.create(mission.repo, mission.base_commit)
        except Exception as exc:  # noqa: BLE001 -- infra failure, fail the task closed
            logger.error("%s/%s: could not create a sandbox: %s", mission.mission_id, task.task_id, exc)
            self._fail_task(mission, task, reason=f"sandbox creation failed: {exc}")
            return

        try:
            objective = contract["objective"]
            prompt = (f"Task {contract['ids']['task']}: {objective}\n"
                     f"Owned paths: {contract.get('owned_paths')}\n"
                     f"Acceptance checks: {contract.get('acceptance_checks')}")
            guard = ToolGuard(workdir=handle.workdir, owned_paths=tuple(contract.get("owned_paths", ())),
                              forbidden_globs=self._policy.floor.forbidden_paths + product.forbidden_paths,
                              protected_globs=self._policy.floor.protected_paths + product.protected_paths)
            gateway_key = self._gateway_key(mission)
            task_budget = product.budgets[f"{lane}_task_usd"]

            attempt = 0
            session_id = None
            while True:
                if session_id is None:
                    result = self._run_agent(role="implementer", product=product, workdir=handle.workdir,
                                             prompt=prompt, gateway_key=gateway_key, budget_usd=task_budget,
                                             max_turns=IMPLEMENT_MAX_TURNS, mission_id=mission.mission_id,
                                             on_tool_approval=guard, contract=contract)
                else:
                    result = self._runtime.resume(session_id, prompt)
                    self._record_agent_spend(mission.mission_id, gateway_key, result.usage_usd)
                session_id = result.session_id or session_id
                self._store.update_task(task.task_id, session_id=session_id)

                if result.status == "budget_exceeded":
                    self._fail_task(mission, task, reason="task budget exhausted")
                    return

                diff = self._capture_diff_clean(handle)
                if not diff.changes:
                    self._fail_task(mission, task, reason="implementer produced no changes")
                    return

                scope_problems = self._reject_out_of_scope_changes(
                    diff, owned_paths=contract.get("owned_paths", ()), workdir=handle.workdir)
                if scope_problems:
                    logger.warning("%s/%s: scope violation: %s", mission.mission_id, task.task_id,
                                  "; ".join(scope_problems))
                    attempt += 1
                    if attempt > max_repairs:
                        self._fail_task(mission, task, reason="; ".join(scope_problems))
                        return
                    self._store.update_task(task.task_id, state="REPAIRING", repair_attempts_used=attempt)
                    prompt = (f"Your last change violated scope: {'; '.join(scope_problems)}. Fix this: stay "
                             f"strictly inside owned_paths {list(contract.get('owned_paths', ()))} and never "
                             f"write a symlink.")
                    self._store.update_task(task.task_id, state="RUNNING")
                    continue

                self._task_diffs[task.task_id] = diff
                classification = classify(diff.changes, self._policy.floor,
                                          product_protected=product.protected_paths,
                                          product_forbidden=product.forbidden_paths)
                if classification.blocked:
                    logger.warning("%s/%s: diff is AC8 (forbidden): %s", mission.mission_id, task.task_id,
                                  "; ".join(classification.reasons))
                    self._store.update_task(task.task_id, state="FAILED")
                    self._boundary(mission, reason=f"forbidden-path diff: {'; '.join(classification.reasons)}")
                    return

                if mission.mandate_id:
                    mandate = next((m for m in product.standing_mandates if m.mandate_id == mission.mandate_id), None)
                    if mandate is not None:
                        coverage = check_diff(mandate, classification=classification, changes=list(diff.changes),
                                              today=self._clock.now().date())
                        if not coverage.covered:
                            logger.info("%s: diff exceeds standing-mandate coverage (%s); back to discovery",
                                      mission.mission_id, "; ".join(coverage.reasons))
                            self._store.update_task(task.task_id, state="FAILED")
                            self._to_discovery(mission, reason="coverage_exceeded")
                            return

                check_results = run_local_checks(self._sandbox, handle,
                                                 {name: product.checks[name] for name in product.verification_required
                                                  if name in product.checks})
                verdict = evaluate_checks(
                    [n for n in product.verification_required if n in product.checks], check_results)
                if verdict.ok:
                    revision = diff.content_hash
                    self._store.update_task(task.task_id, state="DONE", revision=revision)
                    return

                attempt += 1
                if attempt > max_repairs:
                    detail = "; ".join(f"{n}: {check_results[n].detail}" for n in verdict.failing if n in check_results)
                    self._fail_task(mission, task, reason=f"repair attempts exhausted ({detail or verdict.missing})")
                    return

                self._store.update_task(task.task_id, state="REPAIRING", repair_attempts_used=attempt)
                failing_detail = "\n".join(
                    f"- {name}: {check_results[name].detail}" for name in (*verdict.failing, *verdict.missing)
                    if name in check_results)
                prompt = (f"The following checks failed. Fix the code so they pass, staying inside your "
                         f"owned paths:\n{failing_detail or verdict.missing}")
                self._store.update_task(task.task_id, state="RUNNING")
        finally:
            self._sandbox.destroy(handle)

    def _fail_task(self, mission: MissionRecord, task: TaskRecord, *, reason: str) -> None:
        self._store.update_task(task.task_id, state="FAILED")
        self._boundary(mission, reason=reason)

    def _boundary(self, mission: MissionRecord, *, reason: str) -> None:
        mission = self._mission(mission.mission_id)
        if mission.state not in mission_fsm.NO_WORKER:
            mission = self._transition(mission, "boundary")
        product = self._product(mission)
        self._request_hx(mission, product, reason=reason)

    def _to_discovery(self, mission: MissionRecord, *, reason: str) -> None:
        mission = self._mission(mission.mission_id)
        mission = self._transition(mission, "coverage_exceeded")
        product = self._product(mission)
        work_item = self._work_items.get(mission.work_item_id)
        if work_item is None:
            repo, number = self._issue_ref(mission.work_item_id)
            work_item = self._github.get_issue(repo, number)
            work_item = replace(work_item, product=product.name)
        self._run_discovery(mission, product, work_item)

    def _request_hx(self, mission: MissionRecord, product: Product, *, reason: str) -> None:
        required = hx_requirement(self._policy, product.risk_profile)
        self._request_approval(mission, product, gate="HX", required=required,
                               title=packets.hx_title(mission, reason),
                               recommendation=f"REVISE: {reason}", summary=reason,
                               content_hash=mission.content_hash or sha256_text(mission.mission_id),
                               alternatives=packets.HX_ALTERNATIVES,
                               recovery_plan="See alternatives.", cost_usd=mission.spent_usd)

    def _on_hx_approved(self, mission: MissionRecord) -> None:
        new_budget = mission.budget_usd * 1.5 if mission.budget_usd else mission.budget_usd
        # A BLOCKED mission (an earlier HX defer) only ever leaves BLOCKED through
        # a fresh HX approval (`Factory.unblock`, red team #3 item 11); the FSM's
        # dedicated event for that is "hx_approved", not "approved" (which is only
        # valid from AWAITING_HX). Both land in ACTIVE either way.
        event = "hx_approved" if mission.state == "BLOCKED" else "approved"
        mission = self._transition(mission, event, budget_usd=new_budget)
        old_key = self._gateway_keys.get(mission.mission_id)
        if old_key is None:
            old_key = self._recall_key(mission.mission_id, "gateway_key")
        if new_budget and old_key:
            # The extension is meaningless if the LLM gateway still enforces the OLD
            # cap: revoke the old key and mint a new one for the raised budget
            # (red-team #2 item 9). The new key's OWN cap is `new_budget -
            # spent(old_key)` (red team #3 item 4/M1), not `new_budget` again --
            # otherwise total spend across both keys could reach
            # `already_spent + new_budget`, exceeding the extended cap.
            try:
                already_spent = self._budget.spent(old_key)
            except Exception:  # noqa: BLE001 -- if the gateway can't report spend, don't grant it twice
                logger.warning("%s: could not read spend on the old gateway key; granting no headroom",
                              mission.mission_id, exc_info=True)
                already_spent = new_budget
            remaining = max(new_budget - already_spent, 0.0)
            try:
                self._budget.revoke(old_key)
            except Exception:  # noqa: BLE001 -- best-effort; a failed revoke must not block the new key
                logger.warning("%s: could not revoke the old gateway key", mission.mission_id, exc_info=True)
            new_key = self._budget.create_key(mission.mission_id, remaining)
            self._gateway_keys[mission.mission_id] = new_key
            self._remember_key(mission.mission_id, "gateway_key", new_key)
        for task in self._store.list_tasks(mission.mission_id):
            if task.state == "FAILED":
                self._store.update_task(task.task_id, state="READY", repair_attempts_used=0)

    # ── operator entry points (red team #3 item 11) ─────────────────────────────
    # `cli.py` (a separate builder) calls these three by these exact names.
    def kill_switch(self) -> None:
        """Pause every non-terminal, non-HELD mission (the FSM's global
        `kill_switch` event, valid from any state but HELD/terminal). `_transition`'s
        own centralized hook (item 5/M2) then revokes each mission's gateway and
        discovery keys, so nothing can keep spending while the factory is halted."""
        for mission in self._store.list_missions():
            if mission.state == "HELD" or mission.state in mission_fsm.TERMINAL:
                continue
            try:
                self._transition(mission, "kill_switch")
            except (mission_fsm.InvalidTransition, ConcurrentUpdate) as exc:
                logger.warning("%s: kill_switch did not apply: %s", mission.mission_id, exc)

    def resume(self, mission_id: str) -> None:
        """Resume a HELD mission. The FSM's dynamic `resume` event (`Mission.fire`)
        returns it to the gate it was held from if that's still an open human
        decision, or to AWAITING_HX otherwise (a kill switch or an expired hold
        needs a human's go-ahead before work continues either way). Because
        `_gateway_key` is self-healing (item 5/M2), the mission's next agent run
        mints a fresh key rather than reusing the one revoked when it paused."""
        mission = self._mission(mission_id)
        if mission.state != "HELD":
            raise ValueError(f"{mission_id} is {mission.state}, not HELD")
        held_from = mission.held_from
        mission = self._transition(mission, "resume")
        if mission.state == "AWAITING_HX" and held_from not in mission_fsm.AWAITING:
            # Landed in AWAITING_HX as the FSM's fallback (it was paused from a
            # WORK_STATE, not from an existing AWAITING_* gate), so there is no
            # approval request open for it yet -- open one. A resume back to an
            # actual AWAITING_* gate keeps its original, still-open request as is.
            product = self._product(mission)
            self._request_hx(mission, product,
                             reason=f"resumed from HELD (was {held_from or 'unknown'}); needs a human decision")

    def unblock(self, mission_id: str) -> None:
        """A BLOCKED mission (an earlier HX defer) resumes only through a fresh HX
        approval (red team #3 item 11): if none is open yet, this opens one; once
        an eligible approver approves it, the normal quorum-met scan in
        `process_approvals` finds it and `_on_hx_approved` fires the FSM's
        `hx_approved` event (BLOCKED -> ACTIVE)."""
        mission = self._mission(mission_id)
        if mission.state != "BLOCKED":
            raise ValueError(f"{mission_id} is {mission.state}, not BLOCKED")
        marker = self._recall(mission_id, "open_request")
        if marker:
            try:
                existing = self._store.approvals.get(marker["request_id"])
                # Only a request with NO decisions yet is still genuinely awaiting
                # its first one -- `defer`/`revise` leave `status == "open"` (only
                # `revise`/`cancel` void it) but are stale for consuming again:
                # the mission moved on (e.g. AWAITING_HX -> BLOCKED) after it was
                # created, so its `state_version` no longer matches.
                if existing.status == "open" and not existing.decisions:
                    return  # already awaiting a decision on the unblock request
            except (KeyError, NotFound):
                pass
        product = self._product(mission)
        self._request_hx(mission, product, reason="unblock requested")

    # ── 6: join + integrate (final draft §7 "Integration", §9.1) ────────────────
    def _maybe_integrate(self, mission: MissionRecord) -> None:
        if mission.state != "ACTIVE":
            return
        tasks = self._store.list_tasks(mission.mission_id)
        if not tasks or any(t.state not in ("DONE",) for t in tasks):
            return
        mission = self._transition(mission, "tasks_done")
        self._integrate(mission, tasks)

    def _integrate(self, mission: MissionRecord, tasks: list) -> None:
        product = self._product(mission)
        try:
            self._ensure_mirror(mission.repo)
            handle = self._sandbox.create(mission.repo, mission.base_commit)
        except Exception as exc:  # noqa: BLE001
            logger.error("%s: integration sandbox failed: %s", mission.mission_id, exc)
            self._transition(mission, "conflict")
            return

        try:
            for task in tasks:
                diff = self._task_diffs.get(task.task_id)
                if diff is None:
                    logger.warning("%s/%s: no cached diff to integrate (process restarted); "
                                  "re-run this task", mission.mission_id, task.task_id)
                    self._store.update_task(task.task_id, state="READY")
                    self._transition(mission, "conflict")
                    return
                # The patch lives under a dedicated, always-scrubbed scratch directory
                # (`CONTROLLER_TMP_DIRNAME`), never loose in the workdir root -- see
                # `_scrub_controller_artifacts`/`_capture_diff_clean` (red-team #2 item 5).
                # `SandboxPort.exec` has no stdin, so this is the best available way to
                # hand the sandbox this content without it looking like an agent's own
                # file (see README "Requests to orchestrator": exec() could take stdin).
                tmp_dir = os.path.join(handle.workdir, CONTROLLER_TMP_DIRNAME)
                os.makedirs(tmp_dir, exist_ok=True)
                patch_rel = f"{CONTROLLER_TMP_DIRNAME}/integrate.patch"
                patch_path = os.path.join(handle.workdir, patch_rel)
                try:
                    with open(patch_path, "w") as f:
                        f.write(diff.patch)
                    # No `--index`/`--cached`: the checkout's `.git` lives outside
                    # `handle.workdir` (sandbox/base.py hardening), so this applies
                    # straight to the working-tree files as a plain patch tool -- no
                    # repository needed here. `capture_diff` (the real git dir) picks
                    # the result up correctly afterwards.
                    result = self._sandbox.exec(handle, ["git", "apply", "--whitespace=nowarn", patch_rel],
                                                network=False)
                finally:
                    shutil.rmtree(tmp_dir, ignore_errors=True)
                if result.exit_code != 0:
                    logger.warning("%s: integration conflict applying %s: %s", mission.mission_id,
                                  task.task_id, result.stderr)
                    self._store.update_task(task.task_id, state="READY")
                    self._transition(mission, "conflict")
                    return

            combined = self._capture_diff_clean(handle)
            scope_problems = self._reject_out_of_scope_changes(
                combined, owned_paths=self._combined_owned_paths(tasks), workdir=handle.workdir)
            classification = classify(combined.changes, self._policy.floor,
                                      product_protected=product.protected_paths,
                                      product_forbidden=product.forbidden_paths)
            if classification.blocked:
                self._boundary(mission, reason=f"combined diff is AC8: {'; '.join(classification.reasons)}")
                return
            if scope_problems:
                logger.warning("%s: combined diff scope violation: %s", mission.mission_id,
                              "; ".join(scope_problems))
                self._boundary(mission, reason="; ".join(scope_problems))
                return

            check_names = [n for n in product.verification_required if n in product.checks]
            check_results = run_local_checks(self._sandbox, handle,
                                             {n: product.checks[n] for n in check_names})
            review_result = None
            if "review_agent" in product.verification_required:
                review_result = run_review(self._runtime, combined, workdir=handle.workdir,
                                           model=self._model_for(product, "reviewer"), max_turns=REVIEW_MAX_TURNS,
                                           budget_usd=DISCOVERY_BUDGET_USD, gateway_key=self._gateway_key(mission),
                                           gateway_url=self._gateway_url,
                                           system_prompt=load_prompt(self._kit_dir, "reviewer").system_prompt,
                                           on_result=lambda r: self._record_agent_spend(
                                               mission.mission_id, self._gateway_key(mission), r.usage_usd))
                check_results["review_agent"] = review_result

            required_for_merge = [n for n in product.verification_required if n != "holdout_blackbox"]
            verdict = evaluate_checks(required_for_merge, check_results)
        finally:
            self._sandbox.destroy(handle)

        gate = resolve(self._policy, action_class=classification.action_class, risk_profile=product.risk_profile,
                       autonomy_level=product.autonomy_level, protected_touched=bool(classification.protected_touched),
                       covered_by_standing=bool(mission.mandate_id), overrides=product.gate_overrides)
        mission = self._store.update_mission(mission.mission_id, expected_version=mission.state_version,
                                             action_class=classification.action_class,
                                             content_hash=combined.content_hash)

        if not verdict.ok:
            logger.warning("%s: combined checks failed (%s); back to repair", mission.mission_id,
                          "; ".join((*verdict.failing, *verdict.missing)))
            for task in tasks:
                self._store.update_task(task.task_id, state="READY", repair_attempts_used=0)
            self._transition(mission, "conflict")
            return

        evidence = build_evidence(mission_id=mission.mission_id, revision=combined.content_hash,
                                  diff=combined, checks=check_results, action_class=classification.action_class,
                                  rule_fired=gate.rules[-1], evidence_dir=self._evidence_dir,
                                  task_ids=tuple(t.task_id for t in tasks),
                                  cost_usd=mission.spent_usd,
                                  untrusted_inputs=(f"issue body of {mission.work_item_id}",))
        self._store.save_evidence(evidence)

        branch = f"factory/{mission.mission_id}"

        def push():
            return self._github.push_diff(mission.repo, branch=branch, diff=combined,
                                          message=f"{mission.mission_id}: {classification.reasons[-1] if classification.reasons else 'automated change'}",
                                          product_forbidden=product.forbidden_paths,
                                          product_protected=product.protected_paths)

        def probe_push():
            # If a previous attempt already recorded a pushed sha for THIS EXACT
            # combined diff, reuse it instead of pushing (content-addressed) rather
            # than the branch head, since a `factory/<mission>` branch can be
            # legitimately re-pushed by a later revision (red-team #2 item 1).
            cached = self._recall(mission.mission_id, "pushed_sha")
            return cached["sha"] if cached and cached.get("content_hash") == combined.content_hash else None

        push_op = f"push:{mission.mission_id}:{combined.content_hash}"
        sha = execute_once(self._store.intents, push_op, action=push, probe=probe_push)
        self._remember(mission.mission_id, "pushed_sha", {"sha": sha, "content_hash": combined.content_hash})
        self._event(mission.mission_id, telemetry.REVISION_PUSHED, {"sha": sha})

        body = self._pr_body(mission, evidence, gate)

        def probe_open_pr():
            cached = self._recall(mission.mission_id, "pr_number")
            return cached["number"] if cached else None

        pr_op = f"open_pr:{mission.mission_id}:{branch}"
        pr_number = execute_once(self._store.intents, pr_op,
                                 action=lambda: self._github.open_pr(mission.repo, head=branch, base="main",
                                                                     title=f"[factory] {mission.mission_id}", body=body),
                                 probe=probe_open_pr)
        self._remember(mission.mission_id, "pr_number", {"number": pr_number})
        mission = self._store.update_mission(mission.mission_id, expected_version=mission.state_version,
                                             branch=branch, pr_number=pr_number)
        self._resolve_hm(mission, product, gate, sha, evidence)

    def _combined_owned_paths(self, tasks: list) -> tuple:
        paths = {p for t in tasks for p in t.contract.get("owned_paths", ())}
        return tuple(paths) or ("**",)

    def _pr_body(self, mission: MissionRecord, evidence: EvidenceBundle, gate: GateDecision) -> str:
        """A controller-generated PR body (final draft §7). The architect's summary is
        the only agent-authored free text in it, and it is HTML-escaped, fenced and
        length-capped (red-team #2 item 9) -- it is never rendered as anything but a
        quoted block, exactly like the inbox packet treats every untrusted string."""
        checks_table = "\n".join(f"| {name} | {r.conclusion} |" for name, r in evidence.checks.items())
        summary = self._architect_plans.get(mission.mission_id, {}).get("summary", "")
        summary = html.escape(summary)[:PR_SUMMARY_MAX_CHARS]
        return (f"Mission `{mission.mission_id}` (action class {evidence.action_class}, "
               f"rule `{evidence.rule_fired}`)\n\n"
               f"> Summary (agent-authored, quoted verbatim):\n> ```text\n> {summary}\n> ```\n\n"
               f"| check | conclusion |\n|---|---|\n{checks_table}\n\n"
               f"Content hash: `{evidence.content_hash}`")

    # ── 7: HM (final draft §7 "HM: CODEOWNERS PR review") ───────────────────────
    def _resolve_hm(self, mission: MissionRecord, product: Product, gate: GateDecision, head_sha: str,
                    evidence: EvidenceBundle) -> None:
        if gate.hm.kind == "auto":
            # The required checks run as real (or faked) CI on the pushed commit,
            # which hasn't happened yet at this instant -- merging has to wait for
            # a `check_suite completed` event on `head_sha`, same as the PR-review
            # path waits for a human. `_maybe_auto_merge` (driven by that webhook)
            # does the actual merge once every required check is green.
            self._remember(mission.mission_id, "pending_auto_merge", {"sha": head_sha})
            logger.info("%s: HM is auto; waiting for CI on %s before merging", mission.mission_id, head_sha)
            return
        mission = self._transition(mission, "hm_required")
        self._request_approval(mission, product, gate="HM", required=gate.hm,
                               title=f"HM: merge {mission.mission_id}", recommendation="APPROVE: checks green",
                               summary=f"PR #{mission.pr_number} on {mission.repo}", content_hash=mission.content_hash,
                               raw_diff="", evidence=evidence, cost_usd=mission.spent_usd)

    def _required_github_check_names(self, product: Product) -> list:
        """The check NAMES GitHub's own CI reports for this product (its sandboxed
        `checks`, e.g. lint/unit) -- `review_agent`/`holdout_blackbox` are evaluated
        by the controller itself and never appear as GitHub check runs."""
        return list(product.checks.keys())

    def _github_checks_green(self, mission: MissionRecord, product: Product, sha: str) -> bool:
        """Every required check NAME must be present AND `success` on `sha` -- not
        just "every check GitHub happened to report is green" (red-team #2 item 6:
        a required check GitHub hasn't run yet must not be silently treated as
        passing)."""
        results = {c.name: c for c in self._github.check_runs(mission.repo, sha)}
        verdict = evaluate_checks(self._required_github_check_names(product), results)
        if not verdict.ok:
            logger.info("%s: checks on %s not all green (missing=%s failing=%s)",
                       mission.mission_id, sha, verdict.missing, verdict.failing)
        return verdict.ok

    def _maybe_auto_merge(self, mission: MissionRecord, product: Product, sha: str) -> None:
        pending = self._recall(mission.mission_id, "pending_auto_merge")
        if not pending or pending.get("sha") != sha or mission.state != "INTEGRATING":
            return
        if self._github_checks_green(mission, product, sha):
            self._do_merge(mission, product, sha)

    def _on_hm_approved(self, mission: MissionRecord, fencing_token: int) -> None:
        product = self._product(mission)
        # Merge against the sha that was actually reviewed (`on_pull_request_review`
        # already required this for a GitHub-review approval), not a freshly re-fetched
        # PR head that may have moved since (red-team #2 item 2).
        pushed = self._recall(mission.mission_id, "pushed_sha")
        expected_sha = pushed["sha"] if pushed else self._github.pr_head_sha(mission.repo, mission.pr_number)
        self._do_merge(mission, product, expected_sha)

    def _do_merge(self, mission: MissionRecord, product: Product, expected_head_sha: str) -> None:
        op_id = f"merge:{mission.mission_id}:{expected_head_sha}"

        def merge():
            return self._github.merge_pr(mission.repo, mission.pr_number, expected_head_sha=expected_head_sha)

        try:
            merge_sha = execute_once(self._store.intents, op_id, action=merge, probe=lambda: None)
        except MergeConflict as exc:
            # The PR's live head no longer matches what was reviewed/approved -- refuse
            # the merge (red-team #2 item 2). This can only mean someone pushed to the
            # branch after review; record it and send the mission back for a fresh
            # integration pass rather than merging something nobody actually approved.
            logger.warning("%s: merge refused, PR head moved: %s", mission.mission_id, exc)
            self._event(mission.mission_id, telemetry.HUMAN_INTERVENTION,
                       {"action": "pr_head_changed_before_merge", "actor": "unknown"})
            event = "conflict" if mission.state == "INTEGRATING" else "changes_requested"
            mission = self._transition(mission, event)
            self._create_repair_task(mission, product,
                                     reason=f"PR head changed before merge (expected {expected_head_sha})")
            return
        flag = f"mission-{mission.mission_id}"
        self._flags.create(flag)
        mission = self._transition(mission, "approved" if mission.state == "AWAITING_HM" else "hm_auto",
                                   flag=flag, artifact=None)
        self._remember(mission.mission_id, "merge_sha", {"sha": merge_sha})

    # ── 8: post-merge (final draft §7 "Post-merge checks on main") ─────────────
    def _check_post_merge(self, mission: MissionRecord, product: Product, merge_sha: str) -> None:
        if not self._github_checks_green(mission, product, merge_sha):
            # This is a revert on `main` from a post-merge CI failure -- nothing
            # was ever deployed to production. `environment="production"` here
            # would make `compute_dora_metrics` count it as a production incident
            # (its `incident_count` filters DEPLOY_FAILED/REVERTED by environment),
            # inflating change_fail_rate for a release that never shipped (red
            # team #3 item 14). A distinct environment value keeps the event for
            # audit/timeline purposes without it being mistaken for one.
            self._event(mission.mission_id, telemetry.REVERTED,
                       {"environment": "main", "sha": merge_sha})
            mission = self._transition(mission, "post_merge_failure")
            revert_op = f"revert:{mission.mission_id}:{merge_sha}"
            revert_sha = execute_once(self._store.intents, revert_op,
                                      action=lambda: auto_revert(self._github, mission.repo, merge_sha),
                                      probe=lambda: None)
            self._event(mission.mission_id, telemetry.RECOVERED, {"environment": "main"})
            mission = self._transition(mission, "repair")
            self._create_repair_task(mission, product, reason=f"post-merge checks failed on {merge_sha}, "
                                                              f"reverted as {revert_sha}")
            return
        self._on_post_merge_ok(mission, product, merge_sha)

    def _create_repair_task(self, mission: MissionRecord, product: Product, *, reason: str) -> None:
        short_id = f"repair-{uuid.uuid4().hex[:6]}"
        last_tasks = self._store.list_tasks(mission.mission_id)
        owned = sorted({p for t in last_tasks for p in t.contract.get("owned_paths", ())}) or ["**"]
        raw = {"task_id": short_id, "objective": f"Fix forward: {reason}", "owned_paths": owned,
              "action_class": mission.action_class or "AC3",
              "acceptance_checks": list(product.verification_required), "depends_on": []}
        contract = self._build_task_contract(mission, product, raw, run_id="R-repair")
        task_id = f"{mission.mission_id}:{short_id}"
        self._store.create_task(TaskRecord(task_id=task_id, mission_id=mission.mission_id, contract=contract,
                                           state="READY"))

    def _on_post_merge_ok(self, mission: MissionRecord, product: Product, merge_sha: str) -> None:
        mission = self._transition(mission, "evidence_complete")
        self._build_release_candidate(mission, product, merge_sha)

    # ── 9: release candidate (final draft §7 "Build once, deploy staging...") ──
    def _build_release_candidate(self, mission: MissionRecord, product: Product, merge_sha: str) -> None:
        # Keyed by merge_sha, not just mission.mission_id: a repaired revision after a
        # revert/repair cycle must build and deploy the NEW content, not replay the
        # receipt from a previous revision's build (red-team #2 item 1).
        build_op = f"build:{mission.mission_id}:{merge_sha}"
        artifact = execute_once(self._store.intents, build_op,
                                action=lambda: self._deploy.build(mission.repo, merge_sha), probe=lambda: None)
        mission = self._store.update_mission(mission.mission_id, expected_version=mission.state_version,
                                             artifact=artifact)
        # A fresh, globally monotonic token every time (red team #3 C1/H3): a
        # constant would make every release after the first one to any given
        # environment a replay as far as the target's own fencing check is
        # concerned, since a token can never be reused once seen.
        staging_op = f"deploy_staging:{mission.mission_id}:{artifact}"
        staging_token = self._store.next_fencing_token()
        receipt = execute_once(self._store.intents, staging_op,
                               action=lambda: self._deploy.deploy(artifact, environment=STAGING_ENVIRONMENT,
                                                                 operation_id=staging_op,
                                                                 fencing_token=staging_token),
                               probe=lambda: None)
        self._event(mission.mission_id, telemetry.DEPLOYED, {"environment": STAGING_ENVIRONMENT, "artifact": artifact})
        staging_url = self._deploy.url(STAGING_ENVIRONMENT)

        try:
            passed, total = self._holdout.run(product.name, staging_url=staging_url, artifact=artifact)
        except Exception as exc:  # noqa: BLE001 -- fail closed
            logger.warning("%s: holdout run failed: %s", mission.mission_id, exc)
            passed, total = 0, 0
        holdout_ok = total > 0 and passed == total
        holdout_check = CheckResult("holdout_blackbox", "success" if holdout_ok else "failure",
                                    detail=f"{passed}/{total} passed")

        evidence = self._store.get_evidence(mission.mission_id, mission.content_hash)
        checks = dict(evidence.checks) if evidence else {}
        checks["holdout_blackbox"] = holdout_check
        required = list(product.verification_required)
        verdict = evaluate_checks(required, checks)
        if evidence is not None:
            evidence.checks = checks
            evidence.holdout = (passed, total)
            self._store.save_evidence(evidence)

        if not verdict.ok:
            logger.warning("%s: release evidence incomplete (%s); back to repair", mission.mission_id,
                          "; ".join((*verdict.failing, *verdict.missing)))
            self._transition(mission, "evidence_failed")
            self._create_repair_task(mission, product, reason="release evidence failed")
            return

        gate = resolve(self._policy, action_class=mission.action_class or "AC3", risk_profile=product.risk_profile,
                       autonomy_level=product.autonomy_level, covered_by_standing=bool(mission.mandate_id),
                       overrides=product.gate_overrides)
        h2 = release_requirement([gate])
        if h2.kind == "standing":
            mission = self._transition(mission, "h2_standing")
            self._deploy_to_prod(mission, product)
            return
        mission = self._transition(mission, "h2_required")
        self._request_approval(mission, product, gate="H2", required=h2, title=f"H2: release {mission.mission_id}",
                               recommendation="APPROVE: staging + holdout evidence complete",
                               summary=f"{passed}/{total} holdout scenarios passed", content_hash=mission.content_hash,
                               evidence=evidence, cost_usd=mission.spent_usd)

    def _on_h2_approved(self, mission: MissionRecord, fencing_token: int) -> None:
        product = self._product(mission)
        self._deploy_to_prod(mission, product, fencing_token=fencing_token)

    def _deploy_failed_escalate(self, mission: MissionRecord, *, reason: str) -> None:
        """A production deploy that failed (or whose success can't be confirmed)
        can't use the generic `_boundary` (ACTIVE-only): DEPLOYING's own FSM path is
        `deploy_failed` -> RECOVERING -> `outside_authority` -> AWAITING_HX (no
        automated diagnosis in v1, matching `advance_observation`'s regression
        handling)."""
        mission = self._mission(mission.mission_id)
        if mission.state == "DEPLOYING":
            mission = self._transition(mission, "deploy_failed")
        if mission.state == "RECOVERING":
            mission = self._transition(mission, "outside_authority")
        product = self._product(mission)
        self._request_hx(mission, product, reason=reason)

    def _safe_healthy(self, environment: str) -> bool | None:
        try:
            return self._deploy.healthy(environment)
        except Exception:  # noqa: BLE001 -- this is only for a log/evidence message
            return None

    # ── 10: deploy (final draft §7 "Deploy controller consumes approval") ──────
    def _deploy_to_prod(self, mission: MissionRecord, product: Product, fencing_token: int | None = None) -> None:
        if mission.state == "AWAITING_H2":
            mission = self._transition(mission, "approval_consumed")
        # else: already DEPLOYING, having just come from RELEASE_READY via "h2_standing"
        # -- there's no approval to consume a token from, so mint a fresh one from the
        # SAME globally monotonic sequence (red team #3 C1/H3): a constant here would
        # make every standing release after the first one to any environment collide
        # with the previous release's already-used token.
        if fencing_token is None:
            fencing_token = self._store.next_fencing_token()
        # Keyed by artifact (red-team #2 item 1): a later release of the SAME mission
        # (after a repair) deploys a NEW artifact and must not replay an old receipt.
        op_id = f"deploy_prod:{mission.mission_id}:{mission.artifact}"

        def deploy():
            return self._deploy.deploy(mission.artifact, environment=PROD_ENVIRONMENT,
                                       operation_id=op_id, fencing_token=fencing_token)

        try:
            receipt = execute_once(self._store.intents, op_id, action=deploy, probe=lambda: None)
        except StaleApproval as exc:
            # The target's own fencing check rejected this token: NEVER assume the
            # deploy happened anyway (red team #3 C1). The port has no way to ask a
            # target "which artifact is live right now", so the only check available
            # is `healthy` -- which says nothing about WHICH artifact is healthy, so
            # it can't positively confirm this one landed either. Treat it as not
            # applied and escalate with the failure as evidence, rather than letting
            # the mission drift toward DELIVERED for a release that may never have
            # actually reached production.
            healthy = self._safe_healthy(PROD_ENVIRONMENT)
            self._event(mission.mission_id, telemetry.DEPLOY_FAILED,
                       {"environment": PROD_ENVIRONMENT, "artifact": mission.artifact,
                        "reason": f"fencing rejected token {fencing_token}: {exc}"})
            self._deploy_failed_escalate(mission, reason=f"production deploy fencing rejected (target healthy={healthy}); "
                                          f"the release may not have been applied: {exc}")
            return
        except Exception as exc:  # noqa: BLE001
            self._event(mission.mission_id, telemetry.DEPLOY_FAILED,
                       {"environment": PROD_ENVIRONMENT, "artifact": mission.artifact, "reason": str(exc)})
            self._deploy_failed_escalate(mission, reason=f"production deploy failed: {exc}")
            return
        self._event(mission.mission_id, telemetry.DEPLOYED, {"environment": PROD_ENVIRONMENT, "artifact": mission.artifact})
        self._flags.set_rollout(mission.flag, 100)
        self._transition(self._mission(mission.mission_id), "deployed")

    # ── 11: observe (final draft §7 "Observation window healthy?"; worker) ─────
    def advance_observation(self, mission: MissionRecord) -> None:
        product = self._product(mission)
        if self._deploy.healthy(PROD_ENVIRONMENT):
            # `_transition` itself revokes the gateway/discovery keys on entering
            # DELIVERED (red team #3 item 5/M2's centralized hook) -- an explicit
            # revoke here would be redundant, and calling the (now self-healing)
            # `_gateway_key` afterwards would just mint and immediately discard a
            # brand-new key.
            mission = self._transition(mission, "window_healthy")
            self._event(mission.mission_id, telemetry.DELIVERED, {})
            return
        self._event(mission.mission_id, telemetry.DEPLOY_FAILED,
                   {"environment": PROD_ENVIRONMENT, "artifact": mission.artifact, "reason": "regression detected"})
        mission = self._transition(mission, "regression")
        self._flags.kill(mission.flag)
        rollback_op = f"rollback:{mission.mission_id}:{mission.artifact}"
        execute_once(self._store.intents, rollback_op,
                    action=lambda: self._deploy.rollback(PROD_ENVIRONMENT, to_artifact=None,
                                                         operation_id=rollback_op),
                    probe=lambda: None)
        invalidated = self._store.approvals.invalidate_for_artifact(mission.artifact, "post-deploy rollback")
        logger.info("%s: rollback invalidated approvals %s", mission.mission_id, invalidated)
        # v1 has no automated diagnosis/fix path (final draft §7): always escalate.
        mission = self._transition(mission, "outside_authority")
        self._request_hx(mission, product, reason="production regression: rolled back, needs a human decision")

    # ── durable webhook inbox (red team #3 H1) ──────────────────────────────────
    LABELS_THAT_START_A_MISSION = ("factory:patch", "factory:feature")

    def enqueue_webhook(self, delivery_id: str, kind: str, payload: dict) -> bool:
        """Fast, non-blocking, and durable (`store.enqueue_webhook`): called from
        `app.py`'s webhook handler after it has verified the signature AND parsed
        the raw body into a typed event (`github.webhooks.parse_event`). `delivery_id`
        is GitHub's own `X-GitHub-Delivery` header (so a GitHub redelivery is a
        no-op, fixing red team #2's M4). `kind` is the parsed event's class name
        (e.g. "IssueLabeled", "PullRequestReview" -- one of `github.webhooks`'
        dataclasses), and `payload` is `dataclasses.asdict()` of that parsed event,
        NOT the raw GitHub JSON body. `dispatch_webhooks` rebuilds the typed event
        from `(kind, payload)` rather than re-parsing GitHub's JSON, so app.py's
        parse -- including "ignore this event" decisions -- happens exactly once.
        """
        return self._store.enqueue_webhook(delivery_id, kind, payload)

    def dispatch_webhooks(self) -> None:
        """Claim durable webhook deliveries and process each one (red team #3 H1).

        Each delivery's `event` field is the parsed event's class name (set by
        `enqueue_webhook`'s `kind`) and `payload` is that event's field dict, so
        the typed event is rebuilt with `EventClass(**payload)` -- no re-parsing
        of raw GitHub JSON here.

        A delivery that raises is NOT acked -- its lease simply expires and
        `claim_webhooks` hands it out again later (crash safety). A poison message
        (the same delivery failing `MAX_WEBHOOK_ATTEMPTS` times) is recorded as an
        event and acked anyway, so it can't wedge the queue forever.
        """
        from ..github import webhooks as gh_webhooks

        for delivery in self._store.claim_webhooks(limit=50, lease_seconds=300):
            delivery_id = delivery["delivery_id"]
            kind = delivery["event"]
            try:
                event_cls = getattr(gh_webhooks, kind, None)
                if event_cls is None or not (isinstance(event_cls, type) and hasattr(event_cls, "__dataclass_fields__")):
                    raise ValueError(f"unknown webhook event kind {kind!r}")
                parsed = event_cls(**delivery["payload"])
                self._dispatch_parsed_webhook(parsed)
            except Exception:  # noqa: BLE001 -- one bad delivery must not wedge the queue
                logger.exception("webhook dispatch failed for delivery %s (kind=%s)", delivery_id, kind)
                attempts = self._webhook_attempts.get(delivery_id, 0) + 1
                self._webhook_attempts[delivery_id] = attempts
                if attempts < MAX_WEBHOOK_ATTEMPTS:
                    continue  # leave it un-acked; the lease expiry will retry it
                logger.error("webhook delivery %s failed %d times; treating as a poison message",
                            delivery_id, attempts)
                self._event("_webhooks", "webhook_poisoned", {"delivery_id": delivery_id, "kind": kind})
            self._webhook_attempts.pop(delivery_id, None)
            self._store.ack_webhook(delivery_id)

    def _dispatch_parsed_webhook(self, event) -> None:
        from ..github import webhooks as gh_webhooks

        if isinstance(event, gh_webhooks.IssueLabeled):
            self.on_issue_labeled(event)
        elif isinstance(event, gh_webhooks.PullRequestReview):
            self.on_pull_request_review(event)
        elif isinstance(event, gh_webhooks.CheckSuiteCompleted):
            self.on_check_suite_completed(event)
        elif isinstance(event, gh_webhooks.PushToDefault):
            self.on_push_to_default(event)
        elif isinstance(event, gh_webhooks.PushToBranch):
            self.on_push_to_branch(event)
        else:
            logger.debug("ignoring parsed webhook event of type %s", type(event).__name__)

    # ── webhooks (build spec §3 step 13, App §3) ────────────────────────────────
    def on_issue_labeled(self, event) -> MissionRecord | None:
        if event.label not in self.LABELS_THAT_START_A_MISSION:
            return None
        # `RestGitHub.get_issue` sets `WorkItem.product` to the bare repo name;
        # remap it through the product registry's repo -> product mapping
        # (build spec §3 B7 integration note).
        product = self._products.for_repo(event.repo)
        work_item = WorkItem(item_id=f"{event.repo}#{event.number}", product=product.name, repo=event.repo,
                             number=event.number, title=event.title, body=event.body,
                             labels=(event.label,), author=event.author)
        return self.start_mission(work_item)

    _REVIEW_STATE_TO_DECISION = {"APPROVED": "approve", "CHANGES_REQUESTED": "revise"}

    def on_pull_request_review(self, event) -> None:
        decision = self._REVIEW_STATE_TO_DECISION.get(event.state)
        if decision is None:
            return  # COMMENTED, DISMISSED, ...: not a decision either way
        mission = None
        for candidate in self._store.list_missions(state="AWAITING_HM"):
            if candidate.repo == event.repo and candidate.pr_number == event.number:
                mission = candidate
                break
        if mission is None:
            return
        # Bind the review to the exact commit that was pushed and evaluated -- a
        # review submitted on any other sha (e.g. a new commit landed after the
        # reviewer opened the diff) must never count (red-team #2 item 2).
        pushed = self._recall(mission.mission_id, "pushed_sha")
        if not pushed or event.commit_id != pushed["sha"]:
            logger.info("%s: PR review on %s ignored (pushed sha is %s)", mission.mission_id, event.commit_id,
                       pushed and pushed.get("sha"))
            return
        product = self._product(mission)
        if event.reviewer not in product.approvers_for("HM"):
            logger.info("%s: PR review from %s ignored (not a listed HM approver)",
                       mission.mission_id, event.reviewer)
            return
        for req in self._store.approvals.list_open(mission.mission_id):
            if req.gate == "HM":
                roles = product.roles_for(event.reviewer) or ()
                try:
                    self.decide(req.request_id, event.reviewer, roles, decision, req.content_hash)
                except ApprovalError as exc:
                    logger.info("%s: PR review from %s did not apply: %s", mission.mission_id, event.reviewer, exc)
                    continue
                # `on_pull_request_review` only ever runs off the webhook queue (the
                # worker's `dispatch_webhooks`), never inline in a request handler, so
                # it's safe to finish the job here rather than wait for the worker's
                # next tick to call `process_approvals` (build spec §3 B7 "decide").
                self.process_approvals()

    def on_check_suite_completed(self, event) -> None:
        for mission in self._store.list_missions():
            if mission.repo != event.repo:
                continue
            product = self._product(mission)
            if mission.state == "INTEGRATING":
                self._maybe_auto_merge(mission, product, event.sha)
            elif mission.branch and mission.state == "MERGED":
                merged = self._recall(mission.mission_id, "merge_sha")
                if merged and merged.get("sha") == event.sha:
                    self._check_post_merge(mission, product, event.sha)

    def on_push_to_default(self, event) -> None:
        """A human pushing straight to the default branch, outside the approval
        flow. Only ACTIVE missions in this repo are "affected" (their agents are
        working from a `base_commit` that this push just moved past, so the
        eventual re-integration will be against a different main than they
        started from) -- an ADMITTED/AWAITING_*/terminal/etc. mission has no live
        branch this push touches, so it's not a human intervention IN it (red
        team #3 item 14: this used to fire for every mission in the repo)."""
        if event.pusher and "[bot]" not in event.pusher:
            for mission in self._store.list_missions(state="ACTIVE"):
                if mission.repo == event.repo:
                    self._event(mission.mission_id, telemetry.HUMAN_INTERVENTION,
                               {"action": "push_to_default", "actor": event.pusher})

    def on_push_to_branch(self, event) -> None:
        """A human pushing directly to a `factory/*` mission branch (final draft §7
        step 13's human-intervention signal). Recorded as a `human_intervention`
        event, and the pusher is added as a mission editor so they can't later
        approve their own change's HM (`ApprovalStore`'s no-self-approval rule)."""
        if event.pusher_is_bot or not event.pusher:
            return
        for mission in self._store.list_missions():
            if mission.repo == event.repo and mission.branch == event.branch:
                self._event(mission.mission_id, telemetry.HUMAN_INTERVENTION,
                           {"action": "push_to_mission_branch", "actor": event.pusher, "sha": event.sha})
                if event.pusher not in mission.editors:
                    self._store.update_mission(mission.mission_id, expected_version=mission.state_version,
                                               editors=(*mission.editors, event.pusher))


__all__ = ["Factory", "MissionNotFound"]
