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
  - `repos_root/<repo>` git mirrors are assumed to already exist (the demo
    creates them directly; production provisioning is a deployment concern
    outside a `GitHubPort` call -- see "Requests to orchestrator" in the
    final report). B7 never clones over the network itself.
"""
from __future__ import annotations

import logging
import os
import shlex
import threading
import uuid
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path

from .. import globs
from ..agent_output import AgentOutputError, parse_agent_output
from ..controller import mission_fsm
from ..controller.approval_budget import can_admit
from ..controller.approvals import ApprovalError, ApprovalRequest, IneligibleApprover
from ..controller.coverage import CoverageResult, check_diff, check_request
from ..controller.gate_resolver import GateDecision, hx_requirement, release_requirement, resolve
from ..controller.intents import execute_once
from ..models import (CheckResult, Diff, EvidenceBundle, MissionRecord, RuntimeRequest, TaskRecord,
                      WorkItem, sha256_text)
from ..policy import Policy, Requirement
from ..policy.action_classes import Classification, FileChange, classify
from ..ports import (AgentRuntime, BudgetGateway, Clock, ConcurrentUpdate, DeployTarget, FlagProvider,
                     GitHubPort, HoldoutRunner, Notifier, NotFound, SandboxPort, StateStore)
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


class MissionNotFound(NotFound):
    pass


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10]}"


def _repo_mirror_missing(repos_root: str, repo: str) -> bool:
    return not os.path.isdir(os.path.join(repos_root, repo.replace("/", "__")))


class Factory:
    """Orchestrates one product cell's missions from intake to delivery."""

    def __init__(self, *, store: StateStore, github: GitHubPort, runtime: AgentRuntime,
                sandbox: SandboxPort, budget: BudgetGateway, notifier: Notifier,
                holdout: HoldoutRunner, deploy: DeployTarget, flags: FlagProvider,
                products: ProductRegistry, policy: Policy, clock: Clock, kit_dir: str | Path,
                inbox_base_url: str, evidence_dir: str, repos_root: str,
                gateway_url: str | None = None, max_parallel_tasks: int = MAX_PARALLEL_TASKS_DEFAULT):
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
        self._gateway_url = gateway_url
        self._max_parallel_tasks = max_parallel_tasks

        # Process-local caches. Everything durable (mission/task state, approvals,
        # intents, evidence) lives in `store`; these only hold values that are
        # either cheap to recompute (a re-run implementer task) or re-derivable
        # from `store.list_events` after a restart (see `_remember`/`_recall`).
        self._gateway_keys: dict[str, str] = {}
        self._architect_plans: dict[str, dict] = {}
        self._task_diffs: dict[str, Diff] = {}
        self._work_items: dict[str, WorkItem] = {}

        # Webhook events are enqueued fast (by the HTTP handler, on the event loop)
        # and drained by the worker loop (a plain background thread), so a webhook
        # request never blocks on an agent run or a GitHub call. This queue is
        # process-local -- an accepted v1 limitation, see README "v1 scope notes".
        self._webhook_lock = threading.Lock()
        self._webhook_queue: list[tuple[str, object]] = []

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

    def _gateway_key(self, mission: MissionRecord) -> str:
        key = self._gateway_keys.get(mission.mission_id)
        if key is None:
            cached = self._recall(mission.mission_id, "gateway_key")
            key = cached["key"] if cached else None
        if key is None:
            raise RuntimeError(f"{mission.mission_id}: no gateway key on record")
        self._gateway_keys[mission.mission_id] = key
        return key

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
        if mission_id and result.usage_usd:
            self._event(mission_id, telemetry.COST_SPENT, {"usd": result.usage_usd})
            add_spend = getattr(self._budget, "add_spend", None)
            if gateway_key and add_spend:
                try:
                    add_spend(gateway_key, result.usage_usd)
                except Exception as exc:  # noqa: BLE001 -- surfaced by the caller's budget check
                    logger.info("%s: gateway reports budget exceeded: %s", mission_id, exc)
        return result

    def _ensure_mirror(self, repo: str) -> None:
        if _repo_mirror_missing(self._repos_root, repo):
            raise RuntimeError(
                f"no local git mirror for {repo!r} under {self._repos_root!r}; a repo mirror must be "
                f"provisioned before the factory can sandbox it (see README 'Production setup')")

    def _issue_ref(self, work_item_id: str) -> tuple:
        repo, _, number = work_item_id.rpartition("#")
        return repo, int(number)

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
                                     gateway_key=None, budget_usd=DISCOVERY_BUDGET_USD,
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
                                     gateway_key=None, budget_usd=DISCOVERY_BUDGET_USD,
                                     max_turns=DISCOVERY_MAX_TURNS, mission_id=mission.mission_id,
                                     on_tool_approval=guard)
            output = parse_agent_output("architect", result.output_text, kit_dir=self._kit_dir)
        except (AgentOutputError, Exception) as exc:  # noqa: BLE001 -- fail closed, never advance
            logger.warning("%s: architect output invalid (%s); mission stays in DISCOVERING", mission.mission_id, exc)
            self._notifier.info(f"{mission.mission_id}: discovery failed ({exc}); needs manual attention")
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
        request = ApprovalRequest(
            request_id=request_id, gate=gate, mission_ids=(mission.mission_id, *extra_mission_ids),
            operation_id=f"{gate}:{mission.mission_id}", artifact=mission.artifact, content_hash=content_hash,
            policy_version=mission.policy_version, state_version=mission.state_version, required=required,
            risk_profile=product.risk_profile, requester=self._requester(mission),
            expires=expires, editors=frozenset(mission.editors))
        self._store.approvals.add(request)
        packet = packets.build_packet(request_id=request_id, gate=gate, mission=mission, title=title,
                                      recommendation=recommendation, summary=summary, required=str(required),
                                      expires=expires, content_hash=content_hash, raw_diff=raw_diff,
                                      alternatives=alternatives, evidence=evidence, recovery_plan=recovery_plan,
                                      cost_usd=cost_usd, untrusted_inputs=untrusted_inputs,
                                      extra_mission_ids=extra_mission_ids)
        self._store.save_packet(packet)
        self._event(mission.mission_id, telemetry.APPROVAL_REQUESTED, {"gate": gate, "request_id": request_id})
        inbox_url = self._inbox_base_url
        self._notifier.decision_requested(packet, inbox_url=inbox_url)

    def _requester(self, mission: MissionRecord) -> str:
        item = self._work_items.get(mission.work_item_id)
        return (item.author if item and item.author else "factory-bot")

    def decide(self, request_id: str, approver: str, roles, decision: str, content_hash: str) -> ApprovalRequest:
        """The single entry point the inbox and GitHub review webhooks call (build spec §3 step 3)."""
        request = self._store.approvals.decide(request_id, approver=approver, roles=roles, decision=decision,
                                               content_hash=content_hash, now=self._clock.now())
        for mission_id in request.mission_ids:
            self._event(mission_id, telemetry.APPROVAL_DECIDED,
                       {"request_id": request_id, "decision": decision, "approver": approver})

        if decision == "revise":
            self._on_revise(request)
        elif decision == "cancel":
            self._on_cancel(request)
        elif decision == "approve" and request.quorum_met():
            self._on_quorum(request)
        return request

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
            elif request.gate == "HM":
                self._transition(mission, "changes_requested")
            elif request.gate == "H2":
                self._transition(mission, "changes_requested")
            elif request.gate == "HX":
                logger.info("%s: HX revise recorded; awaiting a fresh decision", mission_id)

    def _on_cancel(self, request: ApprovalRequest) -> None:
        for mission_id in request.mission_ids:
            mission = self._mission(mission_id)
            if request.gate == "H1":
                self._transition(mission, "decline")
            elif request.gate == "HX":
                self._transition(mission, "declined")
            else:
                logger.info("%s: %s cancel recorded (no dedicated transition)", mission_id, request.gate)

    def _on_quorum(self, request: ApprovalRequest) -> None:
        mission = self._mission(request.mission_ids[0])
        fencing_token = self._store.approvals.consume(
            request.request_id, executor="factory-controller", now=self._clock.now(),
            state_version=mission.state_version, content_hash=request.content_hash,
            policy_version=mission.policy_version)
        if request.gate == "H1":
            self._on_h1_approved(mission)
        elif request.gate == "HM":
            self._on_hm_approved(mission, fencing_token)
        elif request.gate == "H2":
            self._on_h2_approved(mission, fencing_token)
        elif request.gate == "HX":
            self._on_hx_approved(mission)

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

    def _admit(self, mission: MissionRecord, product: Product, *, tasks: list) -> None:
        lane = mission.lane
        if not can_admit(lane, committed_minutes=0.0, reviewer_hours_per_week=product.budgets["reviewer_hours_per_week"]):
            logger.warning("%s: approval budget exhausted this week; admitting anyway (v1 has no queue)", mission.mission_id)
        budget_usd = product.budgets[f"{lane}_mission_usd"]
        key = self._budget.create_key(mission.mission_id, budget_usd)
        self._gateway_keys[mission.mission_id] = key
        self._remember(mission.mission_id, "gateway_key", {"key": key})

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
        self._transition(mission, "capacity_ok")

    # ── 5: tasks (final draft §7 step "Agents work in sandboxes", §9.1, §14.2) ──
    def run_ready_tasks(self, mission_id: str | None = None) -> None:
        """Release and run every READY task, up to `max_parallel_tasks` at a time.

        Called by the worker loop; the demo also calls it directly to drive a
        scenario forward a step. Tasks are executed sequentially in-process --
        the AND join-barrier semantics (§9.1) are the same as true concurrency
        would give.
        """
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
                session_id = result.session_id or session_id
                self._store.update_task(task.task_id, session_id=session_id)

                if result.status == "budget_exceeded":
                    self._fail_task(mission, task, reason="task budget exhausted")
                    return

                diff = self._sandbox.capture_diff(handle)
                if not diff.changes:
                    self._fail_task(mission, task, reason="implementer produced no changes")
                    return
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
        mission = self._transition(mission, "approved",
                                   budget_usd=mission.budget_usd * 1.5 if mission.budget_usd else mission.budget_usd)
        for task in self._store.list_tasks(mission.mission_id):
            if task.state == "FAILED":
                self._store.update_task(task.task_id, state="READY", repair_attempts_used=0)

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
                patch_path = os.path.join(handle.workdir, ".factory-integrate.patch")
                with open(patch_path, "w") as f:
                    f.write(diff.patch)
                # No `--index`/`--cached`: the checkout's `.git` lives outside `handle.workdir`
                # (build spec/red-team hardening, `sandbox/base.py`), so this applies straight to
                # the working-tree files as a plain patch tool -- no repository needed here.
                # `capture_diff` (via the real git dir) picks the result up correctly afterwards.
                result = self._sandbox.exec(handle, ["git", "apply", "--whitespace=nowarn",
                                                     ".factory-integrate.patch"], network=False)
                os.remove(patch_path)
                if result.exit_code != 0:
                    logger.warning("%s: integration conflict applying %s: %s", mission.mission_id,
                                  task.task_id, result.stderr)
                    self._store.update_task(task.task_id, state="READY")
                    self._transition(mission, "conflict")
                    return

            combined = self._sandbox.capture_diff(handle)
            classification = classify(combined.changes, self._policy.floor,
                                      product_protected=product.protected_paths,
                                      product_forbidden=product.forbidden_paths)
            if classification.blocked:
                self._boundary(mission, reason=f"combined diff is AC8: {'; '.join(classification.reasons)}")
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
                                           system_prompt=load_prompt(self._kit_dir, "reviewer").system_prompt)
                check_results["review_agent"] = review_result
                if review_result.conclusion == "success" and review_result.detail:
                    self._event(mission.mission_id, telemetry.COST_SPENT, {"usd": 0.0})

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
                                          message=f"{mission.mission_id}: {classification.reasons[-1] if classification.reasons else 'automated change'}")

        # v1 has no reliable probe for "was this diff already pushed" (branch heads
        # aren't content-addressed by the port). Duplicate pushes are harmless here:
        # a retry re-applies the same patch to the same branch name. See README
        # "v1 scope notes" on intent reconciliation.
        sha = execute_once(self._store.intents, f"push:{mission.mission_id}", action=push, probe=lambda: None)
        self._event(mission.mission_id, telemetry.REVISION_PUSHED, {"sha": sha})

        body = self._pr_body(mission, evidence, gate)
        pr_number = execute_once(self._store.intents, f"open_pr:{mission.mission_id}",
                                 action=lambda: self._github.open_pr(mission.repo, head=branch, base="main",
                                                                     title=f"[factory] {mission.mission_id}", body=body),
                                 probe=lambda: None)
        mission = self._store.update_mission(mission.mission_id, expected_version=mission.state_version,
                                             branch=branch, pr_number=pr_number)
        self._resolve_hm(mission, product, gate, sha, evidence)

    def _pr_body(self, mission: MissionRecord, evidence: EvidenceBundle, gate: GateDecision) -> str:
        checks_table = "\n".join(f"| {name} | {r.conclusion} |" for name, r in evidence.checks.items())
        summary = self._architect_plans.get(mission.mission_id, {}).get("summary", "")
        return (f"Mission `{mission.mission_id}` (action class {evidence.action_class}, "
               f"rule `{evidence.rule_fired}`)\n\n"
               f"> {summary}\n\n"
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

    def _maybe_auto_merge(self, mission: MissionRecord, product: Product, sha: str) -> None:
        pending = self._recall(mission.mission_id, "pending_auto_merge")
        if not pending or pending.get("sha") != sha or mission.state != "INTEGRATING":
            return
        required_checks = self._github.check_runs(mission.repo, sha)
        ok = bool(required_checks) and all(c.conclusion == "success" for c in required_checks)
        if not ok:
            logger.info("%s: CI on %s not all green yet (or none reported); waiting", mission.mission_id, sha)
            return
        self._do_merge(mission, product, sha)

    def _on_hm_approved(self, mission: MissionRecord, fencing_token: int) -> None:
        product = self._product(mission)
        head_sha = self._github.pr_head_sha(mission.repo, mission.pr_number)
        self._do_merge(mission, product, head_sha)

    def _do_merge(self, mission: MissionRecord, product: Product, head_sha: str) -> None:
        def merge():
            return self._github.merge_pr(mission.repo, mission.pr_number, expected_head_sha=head_sha)

        merge_sha = execute_once(self._store.intents, f"merge:{mission.mission_id}", action=merge, probe=lambda: None)
        flag = f"mission-{mission.mission_id}"
        self._flags.create(flag)
        mission = self._transition(mission, "approved" if mission.state == "AWAITING_HM" else "hm_auto",
                                   flag=flag, artifact=None)
        self._remember(mission.mission_id, "merge_sha", {"sha": merge_sha})

    # ── 8: post-merge (final draft §7 "Post-merge checks on main") ─────────────
    def _check_post_merge(self, mission: MissionRecord, product: Product, merge_sha: str) -> None:
        results = self._github.check_runs(mission.repo, merge_sha)
        ok = bool(results) and all(c.conclusion == "success" for c in results)
        if not ok:
            self._event(mission.mission_id, telemetry.REVERTED, {"environment": PROD_ENVIRONMENT, "sha": merge_sha})
            mission = self._transition(mission, "post_merge_failure")
            revert_sha = auto_revert(self._github, mission.repo, merge_sha)
            self._event(mission.mission_id, telemetry.RECOVERED, {"environment": PROD_ENVIRONMENT})
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
        artifact = execute_once(self._store.intents, f"build:{mission.mission_id}",
                                action=lambda: self._deploy.build(mission.repo, merge_sha), probe=lambda: None)
        mission = self._store.update_mission(mission.mission_id, expected_version=mission.state_version,
                                             artifact=artifact)
        staging_op = f"deploy_staging:{mission.mission_id}"
        receipt = execute_once(self._store.intents, staging_op,
                               action=lambda: self._deploy.deploy(artifact, environment=STAGING_ENVIRONMENT,
                                                                 operation_id=staging_op, fencing_token=1),
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

    # ── 10: deploy (final draft §7 "Deploy controller consumes approval") ──────
    def _deploy_to_prod(self, mission: MissionRecord, product: Product, fencing_token: int = 1) -> None:
        if mission.state == "AWAITING_H2":
            mission = self._transition(mission, "approval_consumed")
        # else: already DEPLOYING, having just come from RELEASE_READY via "h2_standing".
        op_id = f"deploy_prod:{mission.mission_id}"
        try:
            receipt = execute_once(self._store.intents, op_id,
                                   action=lambda: self._deploy.deploy(mission.artifact, environment=PROD_ENVIRONMENT,
                                                                     operation_id=op_id, fencing_token=fencing_token),
                                   probe=lambda: None)
        except Exception as exc:  # noqa: BLE001
            self._event(mission.mission_id, telemetry.DEPLOY_FAILED,
                       {"environment": PROD_ENVIRONMENT, "artifact": mission.artifact, "reason": str(exc)})
            raise
        self._event(mission.mission_id, telemetry.DEPLOYED, {"environment": PROD_ENVIRONMENT, "artifact": mission.artifact})
        self._flags.set_rollout(mission.flag, 100)
        self._transition(self._mission(mission.mission_id), "deployed")

    # ── 11: observe (final draft §7 "Observation window healthy?"; worker) ─────
    def advance_observation(self, mission: MissionRecord) -> None:
        product = self._product(mission)
        if self._deploy.healthy(PROD_ENVIRONMENT):
            mission = self._transition(mission, "window_healthy")
            self._event(mission.mission_id, telemetry.DELIVERED, {})
            self._budget.revoke(self._gateway_key(mission))
            return
        self._event(mission.mission_id, telemetry.DEPLOY_FAILED,
                   {"environment": PROD_ENVIRONMENT, "artifact": mission.artifact, "reason": "regression detected"})
        mission = self._transition(mission, "regression")
        self._flags.kill(mission.flag)
        self._deploy.rollback(PROD_ENVIRONMENT, to_artifact=None, operation_id=f"rollback:{mission.mission_id}")
        invalidated = self._store.approvals.invalidate_for_artifact(mission.artifact, "post-deploy rollback")
        logger.info("%s: rollback invalidated approvals %s", mission.mission_id, invalidated)
        # v1 has no automated diagnosis/fix path (final draft §7): always escalate.
        mission = self._transition(mission, "outside_authority")
        self._request_hx(mission, product, reason="production regression: rolled back, needs a human decision")

    # ── webhook queue: fast enqueue (HTTP handler) / drain + dispatch (worker) ──
    LABELS_THAT_START_A_MISSION = ("factory:patch", "factory:feature")

    def enqueue_webhook(self, kind: str, event) -> None:
        """Fast, non-blocking: called from `app.py`'s webhook handler."""
        with self._webhook_lock:
            self._webhook_queue.append((kind, event))

    def drain_webhooks(self) -> list:
        with self._webhook_lock:
            queued, self._webhook_queue = self._webhook_queue, []
        return queued

    def dispatch_webhooks(self) -> None:
        """Process every queued webhook event. Called by the worker loop."""
        for kind, event in self.drain_webhooks():
            try:
                if kind == "issue_labeled":
                    self.on_issue_labeled(event)
                elif kind == "pull_request_review":
                    self.on_pull_request_review(event)
                elif kind == "check_suite_completed":
                    self.on_check_suite_completed(event)
                elif kind == "push_to_default":
                    self.on_push_to_default(event)
                else:
                    logger.debug("ignoring queued webhook kind %r", kind)
            except Exception:  # noqa: BLE001 -- one bad event must not wedge the worker loop
                logger.exception("webhook dispatch failed for kind=%s", kind)

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

    def on_pull_request_review(self, event) -> None:
        if event.state != "APPROVED":
            return
        mission = None
        for candidate in self._store.list_missions(state="AWAITING_HM"):
            if candidate.repo == event.repo and candidate.pr_number == event.number:
                mission = candidate
                break
        if mission is None:
            return
        product = self._product(mission)
        for req in self._store.approvals.list_open(mission.mission_id):
            if req.gate == "HM":
                roles = product.roles_for(event.reviewer) or ()
                try:
                    self.decide(req.request_id, event.reviewer, roles, "approve", req.content_hash)
                except ApprovalError as exc:
                    logger.info("%s: PR review from %s did not apply: %s", mission.mission_id, event.reviewer, exc)

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
        if event.pusher and "[bot]" not in event.pusher:
            for mission in self._store.list_missions():
                if mission.repo == event.repo:
                    self._event(mission.mission_id, telemetry.HUMAN_INTERVENTION,
                               {"action": "push_to_default", "actor": event.pusher})


__all__ = ["Factory", "MissionNotFound"]
