"""Recoverable mission lifecycle, local readiness, and externally referenced delivery.

State files are evidence records, never authenticated human approvals. This
module does not publish a PR, merge a branch, or execute deployment commands.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import socket
import uuid
from contextlib import contextmanager
from pathlib import Path

from .checks import validate_verification
from .core import (
    CONSTITUTION_PATH,
    FactoryError,
    assert_id,
    asset_path,
    digest,
    git,
    hash_file,
    load_config,
    now,
    private_dir,
    profiles,
    read_json,
    safe_path,
    validate,
    write_bytes,
    write_json,
)
from .evidence import (
    CandidateMonitor,
    candidate_snapshot,
    capture_local_governance,
    fingerprint,
    is_metadata,
    matches_path,
)

HOLD_STATES = {"PAUSED", "BLOCKED"}
TERMINAL_STATES = {"DELIVERED", "RECOVERED", "CANCELED"}
POST_MERGE_STATES = {
    "MERGED",
    "STAGING",
    "AWAITING_RELEASE",
    "DEPLOYING",
    "OBSERVING",
    "DELIVERED",
    "RECOVERING",
    "RECOVERED",
}
PRE_MERGE_STATES = {
    "PROPOSED",
    "PLANNED",
    "IMPLEMENTING",
    "VERIFYING",
    "REVIEWING",
    "READY_PR",
}
ACTIVE_TASK_STATES = {"RUNNING", "VERIFYING"}
PROTECTED_FLOOR = (
    ".factory/CONSTITUTION.md",
    "**/AGENTS.md",
    "**/AGENTS.override.md",
    "**/CLAUDE.md",
    "**/CLAUDE.local.md",
    "factory.json",
    ".gitignore",
    ".gitattributes",
    ".factory/policy.json",
    ".factory/workflow.json",
    ".factory/registry.json",
    ".factory/installation.json",
    ".factory/pyproject.toml",
    ".factory/uv.lock",
    ".factory/run.py",
    ".factory/src/**",
    ".factory/schemas/**",
    ".factory/roles/**",
    ".factory/skills/**",
    ".factory/prompts/**",
    ".factory/models/**",
    ".factory/vendors/**",
    ".factory/templates/**",
    ".factory/docs/**",
    ".claude/**",
    ".codex/**",
    ".agents/**",
    ".github/**",
    ".vscode/**",
)
DELIVERY_FIELDS = {
    "MERGED": (),
    "STAGING": ("artifact_digest",),
    "AWAITING_RELEASE": ("artifact_digest", "staging_ref"),
    "DEPLOYING": ("artifact_digest", "staging_ref", "release_ref", "recovery_ref"),
    "OBSERVING": (
        "artifact_digest",
        "staging_ref",
        "release_ref",
        "recovery_ref",
        "deployment_ref",
    ),
    "DELIVERED": (
        "artifact_digest",
        "staging_ref",
        "release_ref",
        "recovery_ref",
        "deployment_ref",
        "observation",
    ),
    "RECOVERING": ("artifact_digest", "incident_ref", "recovery_ref"),
    "RECOVERED": (
        "artifact_digest",
        "incident_ref",
        "recovery_ref",
        "recovery_observation",
        "follow_up_mission",
    ),
}


def require_text(value, label):
    if not isinstance(value, str) or not value.strip():
        raise FactoryError(f"{label} requires concrete text")
    return value.strip()


def control_json(root, name):
    try:
        return json.loads(asset_path(root, name).read_text())
    except (OSError, ValueError) as exc:
        raise FactoryError(f"Cannot read factory control {name}: {exc}") from exc


def mission_path(id):
    return f".factory/missions/{assert_id(id)}/mission.json"


def load_mission(root, id):
    return validate(root, "mission", read_json(root, mission_path(id)))


def effective_state(mission):
    return mission.get("previous_state") if mission["state"] in HOLD_STATES else mission["state"]


def attempts_used(task):
    return task["attempts"] - task.get("attempt_base", 0)


def exhausted(task, config):
    return attempts_used(task) >= 1 + config["limits"]["repair_attempts"]


@contextmanager
def state_lock(root):
    private_dir(root)
    path = safe_path(root, ".factory/local/state.lock")
    token = uuid.uuid4().hex
    record = {
        "pid": os.getpid(),
        "hostname": socket.gethostname(),
        "token": token,
        "created_at": now(),
    }
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise FactoryError(
            "State is locked; inspect .factory/local/state.lock and recover-lock only after its owner exits"
        ) from exc
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(record, handle)
            handle.flush()
            os.fsync(handle.fileno())
        yield
    finally:
        if path.exists() and read_json(root, ".factory/local/state.lock").get("token") == token:
            path.unlink()


def recover_lock(root):
    path = safe_path(root, ".factory/local/state.lock")
    raw = path.read_bytes()
    value = json.loads(raw)
    if (
        value.get("hostname") != socket.gethostname()
        or type(value.get("pid")) is not int
        or value["pid"] <= 0
    ):
        raise FactoryError("Cannot prove lock owner is absent on this host")
    try:
        os.kill(value["pid"], 0)
    except ProcessLookupError:
        pass
    except PermissionError as exc:
        raise FactoryError("Cannot prove lock owner is absent") from exc
    else:
        raise FactoryError("Lock owner is still running")
    if path.read_bytes() != raw:
        raise FactoryError("Lock changed during inspection")
    path.unlink()
    return {"recovered": True, "previous_owner": value["pid"]}


class Assessment:
    def __init__(self, root, mission, observe_mission=True):
        self.root, self.mission = root, copy.deepcopy(mission)
        directory = f".factory/missions/{mission['id']}"
        paths = [f"{directory}/{name}" for name in ("spec.md", "plan.md", "recovery.md")]
        if observe_mission:
            paths.append(f"{directory}/mission.json")
        self.monitor = CandidateMonitor(
            root,
            paths,
            metadata_prefixes=(
                f"{directory}/results/",
                f"{directory}/evidence/",
                f"{directory}/models/",
                f".factory/local/runs/{mission['id']}/",
            ),
        )
        try:
            self.candidate = fingerprint(root, mission)
            self.monitor.known.update(self.candidate["source_paths"])
            self.records = self.record_snapshot()
        except BaseException:
            self.monitor.close()
            raise

    def record_snapshot(self):
        id = self.mission["id"]
        files = {}
        for directory in (
            f".factory/missions/{id}/results",
            f".factory/missions/{id}/evidence",
            f".factory/missions/{id}/models",
            f".factory/local/runs/{id}",
        ):
            path = safe_path(self.root, directory)
            if path.exists():
                for item in sorted(path.rglob("*")):
                    if item.is_symlink():
                        raise FactoryError("Symlink in evidence record")
                    if item.is_file():
                        relative = item.relative_to(self.root).as_posix()
                        files[relative] = hash_file(self.root, relative)
        for name in ("plan.md", "recovery.md"):
            relative = f".factory/missions/{id}/{name}"
            path = safe_path(self.root, relative)
            files[relative] = hash_file(self.root, relative) if path.exists() else None
        return files

    def assert_current(self, expected=None):
        expected = self.mission if expected is None else expected
        if load_mission(self.root, expected["id"]) != expected:
            raise FactoryError("Mission changed during readiness assessment")
        if (
            fingerprint(self.root, expected)["fingerprint"] != self.candidate["fingerprint"]
            or self.record_snapshot() != self.records
        ):
            raise FactoryError("Candidate or evidence changed during readiness assessment")
        report = self.monitor.report()
        if report["source_changed"] or report["monitoring_uncertain"]:
            raise FactoryError(
                "Candidate or evidence changed during readiness assessment; "
                + "; ".join(report["monitoring_reasons"])
            )

    def finish(self, expected=None):
        # Native observation must be stopped and fully drained before the final
        # snapshot can support readiness or publication.
        self.monitor.close()
        self.assert_current(expected)

    def close(self):
        self.monitor.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def update_mission(root, id, mutator, readiness=False):
    with state_lock(root):
        current = load_mission(root, id)
        # Terminal missions have no outgoing transitions; their records (reviews,
        # decisions, results, delivery) are closed history.
        if current["state"] in TERMINAL_STATES:
            raise FactoryError(f"Mission records are immutable in terminal state {current['state']}")
        assessment = Assessment(root, current, observe_mission=False) if readiness else None
        try:
            value = copy.deepcopy(current)
            mutator(value)
            for field in ("id", "base_commit", "created_at", "governance_snapshot"):
                if value.get(field) != current.get(field):
                    raise FactoryError(f"Immutable mission identity changed: {field}")
            state = effective_state(current)
            frozen = set()
            if state in POST_MERGE_STATES:
                frozen.update(("merge_ref", "ci_ref"))
            if state in {
                "DEPLOYING",
                "OBSERVING",
                "DELIVERED",
                "RECOVERING",
                "RECOVERED",
            }:
                frozen.update(("artifact_digest", "staging_ref", "release_ref"))
            if state in {"DELIVERED", "RECOVERED"}:
                frozen.update(DELIVERY_FIELDS[state])
                frozen.update(
                    (
                        "observation",
                        "recovery_observation",
                        "deployment_ref",
                        "recovery_ref",
                        "incident_ref",
                        "follow_up_mission",
                    )
                )
            for field in frozen:
                if value.get("delivery", {}).get(field) != current.get("delivery", {}).get(field):
                    raise FactoryError(f"Completed or approved delivery evidence is immutable: {field}")
            value["updated_at"], value["version"] = now(), current.get("version", 0) + 1
            validate(root, "mission", value)
            if assessment:
                assessment.finish(current)
            write_json(root, mission_path(id), value)
            try:
                if assessment:
                    assessment.assert_current(value)
            except BaseException:
                if load_mission(root, id) == value:
                    write_json(root, mission_path(id), current)
                raise
            return value
        finally:
            if assessment:
                assessment.close()


def mission_template(root, name, id):
    target = asset_path(root, "templates/" + ("decision.md" if name == "decisions.md" else name))
    return target.read_text() if target.exists() else f"# {name.removesuffix('.md')}\n\nMission: {id}\n"


def create_mission(root, input):
    id = assert_id(input["id"])
    config = load_config(root)
    kind = input.get("kind") or "feature"
    if kind not in config["work_types"]:
        raise FactoryError("Unsupported work type")
    try:
        base = git(root, "rev-parse", "--verify", f"{input.get('base') or 'HEAD'}^{{commit}}")
    except FactoryError as exc:
        raise FactoryError(
            "Mission creation requires an existing Git commit; commit the initial project first"
        ) from exc
    try:
        branch = git(root, "symbolic-ref", "--short", "HEAD")
    except FactoryError:
        branch = "DETACHED"
    with state_lock(root):
        path = safe_path(root, f".factory/missions/{id}")
        if path.exists():
            raise FactoryError(f"Mission already exists: {id}")
        created = now()
        mission = {
            "schema_version": 1,
            "id": id,
            "title": require_text(input.get("title"), "Mission title"),
            "kind": kind,
            "state": "PROPOSED",
            "previous_state": None,
            "version": 0,
            "created_at": created,
            "updated_at": created,
            "profile": config["profile"],
            "base_commit": base,
            "branch": branch,
            "spec_hash": None,
            "constitution_hash": hash_file(root, CONSTITUTION_PATH),
            "governance_snapshot": capture_local_governance(root),
            "tasks": [],
            "decisions": [],
            "evidence": [],
            "reviews": [],
            "blockers": [],
            "delivery": {},
        }
        validate(root, "mission", mission)
        path.mkdir(parents=True)
        try:
            for name in (
                "spec.md",
                "plan.md",
                "decisions.md",
                "handoff.md",
                "recovery.md",
            ):
                write_bytes(
                    root,
                    f".factory/missions/{id}/{name}",
                    mission_template(root, name, id).encode(),
                )
            write_json(root, mission_path(id), mission)
        except BaseException:
            shutil.rmtree(path)
            raise
        return mission


def assert_current_scope(root, mission):
    actual = hash_file(root, f".factory/missions/{mission['id']}/spec.md")
    if mission["spec_hash"] != actual:
        raise FactoryError("Specification changed; accept a new scope before continuing")
    if mission["constitution_hash"] != hash_file(root, CONSTITUTION_PATH):
        raise FactoryError("Constitution changed; reconcile mission before continuing")
    if not any(
        d["kind"] == "scope" and d["subject_hash"] == actual and d["reference"].strip()
        for d in mission["decisions"]
    ):
        raise FactoryError("A scope decision reference bound to the current specification is required")


def _resolve_blockers(mission, resolution):
    for blocker in mission["blockers"]:
        mission.setdefault("resolved_blockers", []).append(
            {"blocker": blocker, "resolution": resolution, "resolved_at": now()}
        )
    mission["blockers"] = []


def accept_scope(root, id):
    def mutate(mission):
        if effective_state(mission) not in PRE_MERGE_STATES:
            raise FactoryError("Scope can only be accepted before merge")
        if any(t["status"] in ACTIVE_TASK_STATES for t in mission["tasks"]):
            raise FactoryError("Stop active tasks before changing scope")
        constitution = hash_file(root, CONSTITUTION_PATH)
        if constitution != mission["constitution_hash"]:
            if mission["kind"] != "maintenance" or not any(
                d["kind"] == "exception" and d["subject_hash"] == constitution and d["reference"].strip()
                for d in mission["decisions"]
            ):
                raise FactoryError(
                    "Constitution changes require maintenance and an exception decision for its exact new hash"
                )
            mission["constitution_hash"] = constitution
        mission["spec_hash"] = hash_file(root, f".factory/missions/{id}/spec.md")
        assert_current_scope(root, mission)
        mission["state"], mission["previous_state"] = "PLANNED", None
        mission.pop("suspended_tasks", None)
        for task in mission["tasks"]:
            task["status"] = "TODO"
        _resolve_blockers(mission, "Scope re-accepted for the current specification")

    return update_mission(root, id, mutate)


def _validate_tasks(mission, config):
    tasks = {t["id"]: t for t in mission["tasks"]}
    if len(tasks) != len(mission["tasks"]):
        raise FactoryError("Duplicate task IDs")
    active, visited = set(), set()

    def visit(task):
        if task["id"] in active:
            raise FactoryError("Task dependency cycle")
        if task["id"] in visited:
            return
        active.add(task["id"])
        for dependency in task["depends_on"]:
            if dependency not in tasks:
                raise FactoryError("Dependencies must name existing tasks")
            visit(tasks[dependency])
        active.remove(task["id"])
        visited.add(task["id"])

    for task in tasks.values():
        if not task["checks"] or any(i not in {c["id"] for c in config["checks"]} for i in task["checks"]):
            raise FactoryError("Task must name configured checks")
        for pattern in task["owned_paths"]:
            matches_path("", pattern)
        visit(task)


def add_task(root, id, input):
    config = load_config(root)

    def mutate(mission):
        if mission["state"] not in {"PROPOSED", "PLANNED", "IMPLEMENTING"}:
            raise FactoryError("Tasks cannot be added in this mission state")
        task = {
            "id": assert_id(input["id"]),
            "title": input["title"],
            "status": "TODO",
            "depends_on": input.get("depends_on", []),
            "owned_paths": input.get("owned_paths", []),
            "checks": input.get("checks", [c["id"] for c in config["checks"] if c["required"]]),
            "attempts": 0,
        }
        if input.get("model_assignment"):
            from .models import resolve_assignment

            resolve_assignment(root, mission, input["model_assignment"], current=True)
            task["model_assignment"] = input["model_assignment"]
        mission["tasks"].append(task)
        _validate_tasks(mission, config)

    return update_mission(root, id, mutate)


def edit_task(root, id, task_id, patch):
    config = load_config(root)

    def mutate(mission):
        held = mission["state"] in HOLD_STATES and effective_state(mission) in {
            "PLANNED",
            "IMPLEMENTING",
            "VERIFYING",
            "REVIEWING",
        }
        if mission["state"] not in {"PLANNED", "IMPLEMENTING"} and not held:
            raise FactoryError("Replan in PLANNED or IMPLEMENTING, or during a premerge hold")
        assert_current_scope(root, mission)
        if any(t["status"] in ACTIVE_TASK_STATES for t in mission["tasks"]):
            raise FactoryError("Stop active tasks before replanning")
        task = next((t for t in mission["tasks"] if t["id"] == task_id), None)
        if not task or task["status"] not in {"TODO", "BLOCKED"}:
            raise FactoryError("Only pending TODO or BLOCKED tasks can be updated")
        if any(t["id"] == task_id for t in mission.get("suspended_tasks", [])):
            raise FactoryError(
                "A paused active task cannot be replanned; resume it or block the mission first"
            )
        reason = require_text(patch.get("reason"), "Task update reason")
        history = [h for h in mission.get("task_history", []) if h["task"]["id"] == task_id]
        if any(h["reason"] == reason for h in history):
            raise FactoryError("Task update reason must differ from earlier updates")
        if set(patch) - {
            "title",
            "depends_on",
            "owned_paths",
            "checks",
            "reason",
            "model_assignment",
        }:
            raise FactoryError("Task identity, status and attempts cannot be edited")
        prior = copy.deepcopy(task)
        for key in ("title", "depends_on", "owned_paths", "checks"):
            if key in patch:
                task[key] = patch[key]
        if "model_assignment" in patch:
            if patch["model_assignment"] is None:
                task.pop("model_assignment", None)
            else:
                from .models import resolve_assignment

                resolve_assignment(root, mission, patch["model_assignment"], current=True)
                task["model_assignment"] = patch["model_assignment"]
        contract = lambda t: digest(
            {k: sorted(set(t.get(k, []))) for k in ("owned_paths", "depends_on", "checks")}
        )
        earlier = {contract(prior)} | {contract(h["task"]) for h in history}
        if contract(task) not in earlier and exhausted(prior, config) and task.get("budget_resets", 0) < 1:
            task["attempt_base"] = task["attempts"]
            task["budget_resets"] = task.get("budget_resets", 0) + 1
        _validate_tasks(mission, config)
        mission.setdefault("task_history", []).append({"task": prior, "reason": reason, "updated_at": now()})

    return update_mission(root, id, mutate)


def _assert_writer(root, mission, task_id):
    if any(t["id"] != task_id and t["status"] in ACTIVE_TASK_STATES for t in mission["tasks"]):
        raise FactoryError("Workspace already has an active writer")
    directory = safe_path(root, ".factory/missions")
    for entry in directory.iterdir():
        if entry.is_dir() and entry.name != mission["id"] and (entry / "mission.json").exists():
            other = load_mission(root, entry.name)
            if any(t["status"] in ACTIVE_TASK_STATES for t in other["tasks"]):
                raise FactoryError(f"Workspace writer occupied by mission {other['id']}")


def _dispatch_task(root, mission, task, config, catalog, append=False):
    if config.get("model_selection", {}).get("mode") == "required" and not task.get("model_assignment"):
        raise FactoryError("Model policy requires a task assignment before execution")
    if not task.get("model_assignment"):
        return
    from .models import dispatch_assignment, model_hash, resolve_assignment

    resolved = resolve_assignment(root, mission, task["model_assignment"], current=True)
    plan, assignment = resolved["plan"], resolved["assignment"]
    if plan["request"]["profile"] not in profiles(config):
        raise FactoryError("Task model profile is not active; reconcile the assignment")
    if not catalog:
        raise FactoryError("A bound task needs --model-catalog with current session availability")
    dispatch_assignment(root, plan, assignment["id"], catalog)
    if append:
        task.setdefault("model_attempts", []).append(
            {
                "attempt": task["attempts"] + 1,
                "assignment_hash": task["model_assignment"]["assignment_hash"],
                "profile": plan["request"]["profile"],
                "harness": plan["request"]["harness"],
                "session_id": catalog["session_id"],
                "catalog_hash": model_hash(catalog),
            }
        )


def transition_task(root, id, task_id, to, catalog=None):
    config = load_config(root)
    workflow = control_json(root, "workflow.json")
    exhaustion = []

    def mutate(mission):
        if mission["state"] not in {"IMPLEMENTING", "VERIFYING", "REVIEWING"}:
            raise FactoryError("Mission is not active")
        if to != "BLOCKED":
            assert_current_scope(root, mission)
        task = next((t for t in mission["tasks"] if t["id"] == task_id), None)
        if not task or to not in workflow["task_transitions"].get(task["status"], []):
            raise FactoryError("Invalid task transition")
        if to == "RUNNING":
            if any(
                next((t["status"] for t in mission["tasks"] if t["id"] == d), None) != "DONE"
                for d in task["depends_on"]
            ):
                raise FactoryError("Task dependencies are incomplete")
            if exhausted(task, config):
                reason = f"Repair attempts exhausted for task {task_id} ({task['attempts']} attempts)"
                for item in mission["tasks"]:
                    if item["id"] == task_id or item["status"] in ACTIVE_TASK_STATES:
                        item["status"] = "BLOCKED"
                mission["blockers"].append(
                    {
                        "state": "BLOCKED",
                        "task": task_id,
                        "reason": reason,
                        "next": "Diagnose the cause and replan the task before resuming",
                        "at": now(),
                    }
                )
                mission["previous_state"], mission["state"] = (
                    mission["state"],
                    "BLOCKED",
                )
                mission.pop("suspended_tasks", None)
                exhaustion.append(reason)
                return
            _assert_writer(root, mission, task_id)
            if task["status"] == "DONE":
                if mission["state"] != "IMPLEMENTING":
                    raise FactoryError("Return the mission to IMPLEMENTING before reopening completed work")
                invalidated = {task_id}
                while True:
                    dependent = [
                        t
                        for t in mission["tasks"]
                        if t["id"] not in invalidated and invalidated.intersection(t["depends_on"])
                    ]
                    if not dependent:
                        break
                    for item in dependent:
                        item["status"] = "TODO"
                        invalidated.add(item["id"])
            _dispatch_task(root, mission, task, config, catalog, append=True)
            task["attempts"] += 1
            task.pop("repair_required", None)
        if to == "DONE":
            if task.get("repair_required"):
                raise FactoryError(
                    f"Task failed verification {task['repair_required']} during this attempt; "
                    "transition it to RUNNING to spend a repair attempt"
                )
            candidate = fingerprint(root, mission)
            checked = validate_verification(root, mission, config, candidate, tasks=[task])
            if not task["checks"] or checked["reasons"]:
                raise FactoryError(
                    "Task completion requires current passing check evidence: "
                    + "; ".join(checked["reasons"])
                )
            # All Python-package task completions require an actual result; this
            # requires evidence for every completed task while preserving record order.
            validate_completed_result(root, mission, task, candidate["fingerprint"], checked["reference"])
        task["status"] = to

    value = update_mission(root, id, mutate, readiness=to == "DONE")
    if exhaustion:
        raise FactoryError(exhaustion[0] + "; task and mission are now BLOCKED")
    return value


def delivery_reasons(mission, state):
    delivery = mission.get("delivery", {})
    reasons = [
        f"Delivery reference required: {f}" for f in DELIVERY_FIELDS.get(state, ()) if not delivery.get(f)
    ]
    artifact = (delivery.get("artifact_digest") or "")[7:]
    if state in {"DEPLOYING", "OBSERVING", "DELIVERED"} and not any(
        d["kind"] == "release"
        and d["subject_hash"] == artifact
        and d["reference"] == delivery.get("release_ref")
        for d in mission["decisions"]
    ):
        reasons.append("Release decision must reference the exact artifact digest")
    if state == "DELIVERED" and delivery.get("observation", {}).get("status") != "healthy":
        reasons.append("Observation is not healthy; record the incident and enter RECOVERING")
    if state in {"RECOVERING", "RECOVERED"} and not any(
        d["kind"] == "recovery"
        and d["subject_hash"] == artifact
        and d["reference"] == delivery.get("recovery_ref")
        for d in mission["decisions"]
    ):
        reasons.append("Recovery requires a decision referencing recovery_ref for the exact artifact digest")
    if state == "RECOVERED" and delivery.get("recovery_observation", {}).get("status") != "healthy":
        reasons.append("Recovered state requires a healthy post-recovery observation")
    return reasons


def transition_mission(root, id, to, reason=None, next=None, decision=None):
    config = load_config(root)
    workflow = control_json(root, "workflow.json")
    if to in HOLD_STATES | {"CANCELED"}:
        reason = require_text(reason, f"{to} transition reason")
    if next is not None:
        next = require_text(next, "Next action")

    def mutate(mission):
        if to not in workflow["mission_transitions"].get(mission["state"], []):
            raise FactoryError(f"Invalid transition {mission['state']} -> {to}")
        if to == "PLANNED":
            mission["spec_hash"] = hash_file(root, f".factory/missions/{id}/spec.md")
            assert_current_scope(root, mission)
        if to in {"IMPLEMENTING", "VERIFYING", "REVIEWING", "READY_PR"}:
            assert_current_scope(root, mission)
            if not mission["tasks"]:
                raise FactoryError("At least one task is required")
        if to == "READY_PR":
            gate = assess_gate(root, id)
            if not gate["pass"]:
                raise FactoryError("Readiness gate failed: " + "; ".join(gate["reasons"]))
        if to == "MERGED":
            gate = assess_merged(root, id, mission=mission)
            if not gate["pass"]:
                raise FactoryError("Merge verification failed: " + "; ".join(gate["reasons"]))
        if to in POST_MERGE_STATES - {"MERGED"}:
            if not config["delivery"]["enabled"]:
                raise FactoryError("Delivery is disabled for this product")
            reasons = delivery_reasons(mission, to)
            if to != "RECOVERING":
                reasons += assess_merged(root, id, mission=mission)["reasons"]
            if reasons:
                raise FactoryError("Delivery verification failed: " + "; ".join(reasons))
        if reason:
            mission["blockers"].append(
                {
                    "state": to,
                    "reason": reason,
                    **({"next": next} if next else {}),
                    "at": now(),
                }
            )
        if to == "CANCELED" and decision is not None:
            record = {
                "id": "D-DECLINE-" + uuid.uuid4().hex[:12],
                "kind": "decline",
                "reference": require_text(decision, "Decline decision"),
                "subject_hash": mission["spec_hash"] or hash_file(root, f".factory/missions/{id}/spec.md"),
                "recorded_at": now(),
            }
            mission["decisions"].append(record)
        if to in HOLD_STATES:
            mission["previous_state"] = mission["state"]
        if to == "PAUSED":
            mission["suspended_tasks"] = [
                {"id": t["id"], "status": t["status"]}
                for t in mission["tasks"]
                if t["status"] in ACTIVE_TASK_STATES
            ]
        if to in {"BLOCKED", "CANCELED"}:
            mission.pop("suspended_tasks", None)
        if to in HOLD_STATES | {"CANCELED"}:
            for task in mission["tasks"]:
                if task["status"] in ACTIVE_TASK_STATES:
                    task["status"] = "BLOCKED"
        mission["state"] = to

    result = update_mission(root, id, mutate, readiness=to == "READY_PR")
    if to == "CANCELED":
        try:
            result["handoff_packet"] = create_packet(root, id, "handoff")["path"]
        except (FactoryError, OSError) as exc:
            result["handoff_packet_error"] = str(exc)
    return result


def resume_mission(root, id, to, resolution=None, catalog=None, replan_models=False):
    resolution = require_text(resolution, "Resume resolution")
    config = load_config(root)

    def mutate(mission):
        repair = to == "IMPLEMENTING" and mission.get("previous_state") in PRE_MERGE_STATES - {"PROPOSED"}
        if (
            mission["state"] not in HOLD_STATES
            or (not repair and mission.get("previous_state") != to)
            or to in HOLD_STATES | TERMINAL_STATES
        ):
            raise FactoryError(
                "Resume must name the previous active state, or IMPLEMENTING for premerge repair"
            )
        if mission["spec_hash"]:
            assert_current_scope(root, mission)
        suspended = mission.get("suspended_tasks", []) if mission["state"] == "PAUSED" else []
        if replan_models and (
            mission["state"] != "PAUSED"
            or to != "IMPLEMENTING"
            or not any(
                t.get("model_assignment") and t["id"] in {s["id"] for s in suspended}
                for t in mission["tasks"]
            )
        ):
            raise FactoryError(
                "--replan-models requires a paused premerge bound task and IMPLEMENTING target"
            )
        for task in mission["tasks"]:
            if (
                task["status"] in {"TODO", "BLOCKED"}
                and task["id"] not in {s["id"] for s in suspended}
                and exhausted(task, config)
            ):
                raise FactoryError(
                    f"Task {task['id']} repair attempts are still exhausted; replan its contract before resuming"
                )
        if len(suspended) > 1:
            raise FactoryError("Paused record contains multiple workspace writers")
        for saved in [] if replan_models else suspended:
            task = next((t for t in mission["tasks"] if t["id"] == saved["id"]), None)
            if (
                not task
                or task["status"] != "BLOCKED"
                or any(
                    next((t["status"] for t in mission["tasks"] if t["id"] == d), None) != "DONE"
                    for d in task["depends_on"]
                )
            ):
                raise FactoryError("Paused task dependencies and status need reconciliation")
            _assert_writer(root, mission, task["id"])
            _dispatch_task(root, mission, task, config, catalog)
            task["status"] = saved["status"]
        if to == "READY_PR":
            gate = assess_gate(root, id, resuming_to=to)
            if not gate["pass"]:
                raise FactoryError("Cannot resume stale readiness: " + "; ".join(gate["reasons"]))
        if to in POST_MERGE_STATES:
            gate = assess_delivery(root, id, mission=mission, state=to)
            if not gate["pass"]:
                raise FactoryError("Cannot resume unverified delivery: " + "; ".join(gate["reasons"]))
        _resolve_blockers(mission, resolution)
        mission["state"], mission["previous_state"] = to, None
        mission.pop("suspended_tasks", None)

    return update_mission(root, id, mutate, readiness=to == "READY_PR")


def result_index(root, id):
    file = f".factory/missions/{assert_id(id)}/results/index.json"
    if not safe_path(root, file).exists():
        return {"schema_version": 1, "records": {}}
    return validate(root, "result-index", read_json(root, file))


def load_result(root, mission_id, task_id, supplied_index=None):
    assert_id(mission_id)
    assert_id(task_id)
    index = result_index(root, mission_id) if supplied_index is None else supplied_index
    entry = index["records"].get(task_id)
    if entry:
        path = f".factory/missions/{mission_id}/results/records/{entry['file']}"
        if hash_file(root, path) != entry["sha256"]:
            raise FactoryError(f"Indexed result {task_id} content hash mismatch")
    else:
        raise FactoryError(f"Task {task_id} has no indexed result")
    result = read_json(root, path)
    if not isinstance(result, dict):
        raise FactoryError("Stored result must be an object")
    if type(result.get("execution_attempt")) is not int or result["execution_attempt"] < 1:
        raise FactoryError("Stored result has no valid execution attempt")
    return result


def _validate_observation(root, mission, task, observation):
    if task.get("model_assignment") or observation is not None:
        from .models import validate_task_observation

        validate_task_observation(root, mission, task, observation)


def validate_completed_result(root, mission, task, candidate_fingerprint, reference):
    result = validate(root, "result", load_result(root, mission["id"], task["id"]))
    _validate_observation(root, mission, task, result.get("model_observation"))
    if (
        result["mission_id"] != mission["id"]
        or result["task_id"] != task["id"]
        or result["fingerprint"] != candidate_fingerprint
        or result["status"] != "complete"
        or result["unresolved"]
    ):
        raise FactoryError("Result is incomplete, stale, or belongs to another task")
    if result.get("execution_attempt") != task["attempts"]:
        raise FactoryError("Result execution attempt is stale for the current task")
    if not reference or reference not in result["evidence"]:
        raise FactoryError("Result does not reference current verification evidence")
    if any(c not in result["checks"] for c in task["checks"]):
        raise FactoryError("Result omits assigned checks")
    return result


def record_results(root, id, records):
    if not isinstance(records, list) or not records:
        raise FactoryError("Results must be a nonempty array")
    records = copy.deepcopy(records)
    with state_lock(root):
        mission = load_mission(root, id)
        if effective_state(mission) in POST_MERGE_STATES | TERMINAL_STATES:
            raise FactoryError("Task result evidence is immutable after merge or terminal completion")
        with CandidateMonitor(
            root,
            metadata_prefixes=(
                f".factory/missions/{id}/spec.md",
                f".factory/missions/{id}/models/",
            ),
        ) as monitor:
            candidate = fingerprint(root, mission)
            monitor.known.update(candidate["source_paths"])
            seen = set()
            for record in records:
                validate(root, "result", record)
                if record["mission_id"] != id or record["task_id"] in seen:
                    raise FactoryError("Result mission mismatch or duplicate task in batch")
                seen.add(record["task_id"])
                task = next((t for t in mission["tasks"] if t["id"] == record["task_id"]), None)
                if not task:
                    raise FactoryError("Result task is unknown")
                if task["attempts"] < 1:
                    raise FactoryError("A task must begin execution before a result can be recorded")
                if record.get("execution_attempt", task["attempts"]) != task["attempts"]:
                    raise FactoryError("Result execution attempt does not match current task attempt")
                record["execution_attempt"] = task["attempts"]
                _validate_observation(root, mission, task, record.get("model_observation"))
                if record["fingerprint"] != candidate["fingerprint"]:
                    raise FactoryError("Result fingerprint is stale")
            index = result_index(root, id)
            before_index = copy.deepcopy(index)
            created = []
            try:
                for record in records:
                    name = f"{record['task_id']}-{uuid.uuid4().hex}.json"
                    file = f".factory/missions/{id}/results/records/{name}"
                    write_json(root, file, record)
                    created.append(file)
                    index["records"][record["task_id"]] = {
                        "file": name,
                        "sha256": hash_file(root, file),
                    }
                monitor.close()
                report = monitor.report()
                if (
                    load_mission(root, id) != mission
                    or result_index(root, id) != before_index
                    or fingerprint(root, mission)["fingerprint"] != candidate["fingerprint"]
                    or report["source_changed"]
                    or report["monitoring_uncertain"]
                ):
                    raise FactoryError("Candidate or mission changed during result registration")
                validate(root, "result-index", index)
                write_json(root, f".factory/missions/{id}/results/index.json", index)
                return {
                    "recorded": created,
                    "fingerprint": candidate["fingerprint"],
                    "trust": "local-unattested",
                }
            except BaseException:
                for path in created:
                    safe_path(root, path).unlink(missing_ok=True)
                raise


def record_result(root, id, record):
    result = record_results(root, id, [record])
    return {"recorded": result["recorded"][0], "trust": result["trust"]}


def register_model_plan(root, id, plan):
    from .models import model_hash, validate_plan

    validate_plan(root, plan)
    with state_lock(root):
        mission = load_mission(root, id)
        if effective_state(mission) not in PRE_MERGE_STATES - {"READY_PR"}:
            raise FactoryError("Model plans can only be registered during premerge work")
        file = f".factory/missions/{id}/models/{assert_id(plan['id'])}.json"
        target = safe_path(root, file)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            raise FactoryError("Model plan already registered; use a new plan ID") from exc
        with os.fdopen(fd, "w") as handle:
            json.dump(plan, handle, indent=2)
            handle.write("\n")
        plan_hash = hash_file(root, file)
        return {
            "registered": file,
            "assignments": [
                {
                    "plan_path": file,
                    "plan_hash": plan_hash,
                    "assignment_id": a["id"],
                    "assignment_hash": model_hash(a),
                }
                for a in plan["assignments"]
            ],
            "trust": "local-unattested",
        }


def finding_id(review, position):
    """A finding's id, or a positional id for id-less findings from 0.2.0 reviews.

    Release 0.2.0 recorded findings without ids. Such a historical
    blocking finding stays outstanding until a later review records a resolution
    naming "<review id>-F<1-based position>"; new reviews cannot reuse that id.
    """
    return review["findings"][position].get("id") or f"{review['id']}-F{position + 1}"


def outstanding_findings(reviews):
    """Blocking findings not resolved by a later review, as {finding id: review id}."""
    outstanding = {}
    for review in reviews:
        for resolution in review.get("resolutions", []):
            outstanding.pop(resolution["finding"], None)
        for position, finding in enumerate(review["findings"]):
            if finding["severity"] == "blocking":
                outstanding[finding_id(review, position)] = review["id"]
    return outstanding


def review_reasons(
    mission, fingerprint, stale="A current passing independent review without blocking findings is required"
):
    review = next(iter(mission["reviews"][-1:]), None)
    reasons = []
    if (
        not review
        or review["fingerprint"] != fingerprint
        or review["status"] != "pass"
        or any(f["severity"] == "blocking" for f in review["findings"])
    ):
        reasons.append(stale)
    reasons += [
        f"Blocking finding {finding} from review {origin} has no recorded resolution in a later review"
        for finding, origin in outstanding_findings(mission["reviews"]).items()
    ]
    return reasons


def record_decision(root, id, record):
    def mutate(mission):
        if any(d["id"] == record.get("id") for d in mission["decisions"]):
            raise FactoryError("Duplicate decision ID")
        require_text(record.get("reference"), "Decision external reference")
        mission["decisions"].append({**record, "recorded_at": now()})

    return update_mission(root, id, mutate)


def record_review(root, id, record):
    def mutate(mission):
        # Review belongs to REVIEWING; READY_PR also accepts a review so a reviewer
        # can still reject (or re-pass) the candidate before merge.
        if mission["state"] not in {"REVIEWING", "READY_PR"}:
            raise FactoryError(
                f"Reviews can only be recorded in REVIEWING or READY_PR, not {mission['state']}"
            )
        if not isinstance(record, dict) or any(r["id"] == record.get("id") for r in mission["reviews"]):
            raise FactoryError("Review input must be an object with a new review ID")
        earlier = copy.deepcopy(mission["reviews"])
        mission["reviews"].append({**record, "created_at": now()})
        validate(root, "mission", mission)
        known = {finding_id(r, n) for r in earlier for n in range(len(r["findings"]))}
        for finding in record["findings"]:
            fid = finding.get("id")
            if finding["severity"] == "blocking" and not fid:
                raise FactoryError("A blocking finding needs an id so a later review can resolve it")
            if fid is None:
                continue
            if fid in known:
                raise FactoryError(f"Duplicate review finding id: {fid}")
            known.add(fid)
        outstanding = outstanding_findings(earlier)
        resolved = set()
        for resolution in record.get("resolutions", []):
            if resolution["finding"] not in outstanding or resolution["finding"] in resolved:
                raise FactoryError(
                    f"Resolution must name an unresolved blocking finding: {resolution['finding']}"
                )
            require_text(resolution["reason"], "Finding resolution reason")
            resolved.add(resolution["finding"])
        require_text(record.get("author"), "Independent reviewer identity")
        config = load_config(root)
        if config.get("owners", {}).get("maintainer") and record["author"] == config["owners"]["maintainer"]:
            raise FactoryError("Independent reviewer must differ from the configured implementing maintainer")

    return update_mission(root, id, mutate)


def record_delivery(root, id, record):
    if not isinstance(record, dict) or "ci_ref" in record:
        raise FactoryError("Delivery input must be an object; record CI results with ci-result")
    return update_mission(root, id, lambda m: m.setdefault("delivery", {}).update(record))


def risks_section(markdown):
    lines = markdown.splitlines()
    for position, line in enumerate(lines):
        if re.fullmatch(r"#{2,3}\s+Risks\s*", line, re.IGNORECASE):
            level = len(line.split(" ")[0])
            end = next(
                (
                    n
                    for n in range(position + 1, len(lines))
                    if re.match(r"^#{1," + str(level) + r"}\s", lines[n])
                ),
                len(lines),
            )
            return "\n".join(lines[position + 1 : end]).strip()
    return ""


def pr_prerequisites(root, id):
    risks = None
    for name in ("plan.md", "spec.md"):
        target = safe_path(root, f".factory/missions/{id}/{name}")
        text = target.read_text() if target.exists() else ""
        body = risks_section(text)
        if body and body != risks_section(mission_template(root, name, id)):
            risks = {"source": name, "body": body}
            break
    if risks is None:
        raise FactoryError('plan.md or spec.md needs an authored, nonempty "## Risks" section')
    recovery = safe_path(root, f".factory/missions/{id}/recovery.md")
    text = recovery.read_text() if recovery.exists() else ""
    if not text.strip() or text == mission_template(root, "recovery.md", id):
        raise FactoryError(
            "recovery.md is missing or still the unedited template; state recovery implications"
        )
    return {"risks": risks, "recovery": text}


def _policy(root, mission):
    current = control_json(root, "policy.json")
    try:
        baseline = json.loads(git(root, "show", f"{mission['base_commit']}:.factory/policy.json"))
    except FactoryError:
        baseline = {}
    except ValueError as exc:
        raise FactoryError("Baseline policy is invalid JSON") from exc
    for value in (current, baseline):
        for key in ("protected_paths", "sensitive_paths", "required_decisions"):
            if key in value and (
                not isinstance(value[key], list) or any(not isinstance(item, str) for item in value[key])
            ):
                raise FactoryError(f"Invalid policy {key}")
    return current, baseline


def check_definition_changes(root, mission, config):
    """Weakened product checks since the mission base: removal, argv/cwd change, required->optional.

    Guarded checks are baseline setup steps, baseline-required checks and checks
    named by a task. Adding checks or making one required is never a change here.
    """
    # Only a baseline without factory.json has nothing to compare; any other Git
    # failure propagates so the gate fails closed.
    if not git(root, "ls-tree", "--name-only", mission["base_commit"], "--", "factory.json"):
        return []
    try:
        baseline = json.loads(git(root, "show", f"{mission['base_commit']}:factory.json"))
    except ValueError as exc:
        raise FactoryError("Baseline factory.json is invalid JSON") from exc
    if not isinstance(baseline, dict):
        raise FactoryError("Baseline factory.json is not an object")
    tasked = {c for task in mission["tasks"] for c in task["checks"]}
    changes = []
    for phase, before, after in (
        ("check", baseline.get("checks", []), config["checks"]),
        ("setup", baseline.get("setup", []), config.get("setup", [])),
    ):
        if not isinstance(before, list) or any(not isinstance(c, dict) or "id" not in c for c in before):
            raise FactoryError(f"Baseline factory.json has invalid {phase} definitions")
        current = {c["id"]: c for c in after}
        for item in before:
            if phase == "check" and not item.get("required") and item["id"] not in tasked:
                continue
            now_ = current.get(item["id"])
            if now_ is None:
                changes.append(f"{phase} {item['id']} was removed")
                continue
            if now_["command"] != item.get("command"):
                changes.append(
                    f"{phase} {item['id']} command changed from {item.get('command')} to {now_['command']}"
                )
            if now_.get("cwd", ".") != item.get("cwd", "."):
                changes.append(
                    f"{phase} {item['id']} cwd changed from {item.get('cwd', '.')} to {now_.get('cwd', '.')}"
                )
            if phase == "check" and item.get("required") and not now_["required"]:
                changes.append(f"check {item['id']} changed from required to optional")
    return changes


def _assess_gate_snapshot(root, mission, candidate, resuming_to=None):
    mission = copy.deepcopy(mission)
    reasons = []
    config = load_config(root)
    if resuming_to is not None:
        if (
            mission["state"] not in HOLD_STATES
            or mission.get("previous_state") != resuming_to
            or resuming_to != "READY_PR"
        ):
            raise FactoryError("Invalid readiness resume context")
        mission["state"] = resuming_to
    try:
        pr_prerequisites(root, mission["id"])
    except (FactoryError, OSError) as exc:
        reasons.append(str(exc))
    # Generated vendor files remain protected. Profile changes during a product
    # mission intentionally require maintenance until an exact delta proof exists.
    if safe_path(root, "factory.lock.json").exists():
        try:
            from .rendering import render

            rendered = render(root, check=True)
            if rendered.get("pass") is False or rendered.get("ok") is False:
                reasons.append("Generated profile files differ from current canonical sources")
        except (FactoryError, OSError, ImportError) as exc:
            reasons.append(f"Profile configuration validation failed: {exc}")
    if candidate["unmerged_paths"]:
        reasons.append("Repository contains unresolved merge conflicts")
    current, baseline = _policy(root, mission)
    protected = (
        set(PROTECTED_FLOOR)
        | set(current.get("protected_paths", []))
        | set(baseline.get("protected_paths", []))
    )
    sensitive = set(current.get("sensitive_paths", [])) | set(baseline.get("sensitive_paths", []))
    if mission["spec_hash"] != candidate["spec_hash"]:
        reasons.append("Specification is unaccepted or changed since scope acceptance")
    if mission["constitution_hash"] != hash_file(root, CONSTITUTION_PATH):
        reasons.append("Constitution changed since mission acceptance")
    for kind in (
        {"scope"} | set(current.get("required_decisions", [])) | set(baseline.get("required_decisions", []))
    ):
        subject = candidate["spec_hash"] if kind == "scope" else candidate["fingerprint"]
        if not any(
            d["kind"] == kind and d["subject_hash"] == subject and d["reference"].strip()
            for d in mission["decisions"]
        ):
            reasons.append(
                f"Missing current {kind} decision reference (local records are not authenticated approvals)"
            )
    if not mission["tasks"]:
        reasons.append("Mission has no tasks")
    try:
        _validate_tasks(mission, config)
    except FactoryError as exc:
        reasons.append(str(exc))
    for task in mission["tasks"]:
        if task["status"] != "DONE":
            reasons.append(f"Task {task['id']} is {task['status']}, not DONE")
        if attempts_used(task) > 1 + config["limits"]["repair_attempts"]:
            reasons.append(f"Task {task['id']} exceeded repair limit")
        if config.get("model_selection", {}).get("mode") == "required" and not task.get("model_assignment"):
            reasons.append(f"Task {task['id']} has no required model assignment")
    if mission["blockers"] and resuming_to is None:
        reasons.append("Mission has unresolved blockers")
    if mission["state"] in HOLD_STATES | {"CANCELED"}:
        reasons.append(f"Mission is {mission['state']}")
    owned = [p for task in mission["tasks"] for p in task["owned_paths"]]
    for file in candidate["changed_paths"]:
        if file.startswith(".factory/missions/"):
            reasons.append(f"Unrecognized file in mission records is part of the candidate: {file}")
        if not any(matches_path(file, p) for p in owned):
            reasons.append(f"Changed path outside assigned task scope: {file}")
        if mission["kind"] != "maintenance" and any(matches_path(file, p) for p in protected):
            reasons.append(f"Protected factory path requires a maintenance mission: {file}")
        decision_kind = current.get("sensitive_decision", "exception")
        if any(matches_path(file, p) for p in sensitive) and not any(
            d["kind"] == decision_kind
            and d["subject_hash"] == candidate["fingerprint"]
            and d["reference"].strip()
            for d in mission["decisions"]
        ):
            reasons.append(f"Sensitive path needs a current {decision_kind} decision reference: {file}")
    # Evidence is validated against the current check definitions, so a mission
    # of any kind that weakens them needs an exception bound to this candidate.
    try:
        check_changes = check_definition_changes(root, mission, config)
    except FactoryError as exc:
        check_changes = []
        reasons.append(f"Cannot compare check definitions with the mission base: {exc}")
    if check_changes and not any(
        d["kind"] == "exception" and d["subject_hash"] == candidate["fingerprint"] and d["reference"].strip()
        for d in mission["decisions"]
    ):
        reasons += [
            f"Check definition weakened without a current exception decision: {c}" for c in check_changes
        ]
    for task in mission["tasks"]:
        if task.get("repair_required"):
            reasons.append(
                f"Task {task['id']} failed verification {task['repair_required']}; reopen it through RUNNING"
            )
    checked = validate_verification(root, mission, config, candidate)
    reasons += checked["reasons"]
    review = next(iter(mission["reviews"][-1:]), None)
    reviewed = review_reasons(mission, candidate["fingerprint"])
    reasons += reviewed
    if not reviewed and config.get("owners", {}).get("maintainer") == review["author"]:
        reasons.append("Independent reviewer must differ from implementing maintainer")
    reported = set()
    for task in mission["tasks"]:
        try:
            result = validate_completed_result(
                root, mission, task, candidate["fingerprint"], checked["reference"]
            )
            for file in result["changed_files"]:
                if file not in candidate["changed_paths"] or not any(
                    matches_path(file, pattern) for pattern in task["owned_paths"]
                ):
                    raise FactoryError(f"Result includes unowned or unchanged file: {file}")
                reported.add(file)
        except (FactoryError, OSError) as exc:
            reasons.append(f"Task {task['id']} result is invalid: {exc}")
    for file in candidate["changed_paths"]:
        if file not in reported:
            reasons.append(f"Changed file has no current task result: {file}")
    return {
        "pass": not reasons,
        "reasons": reasons,
        "fingerprint": candidate["fingerprint"],
        "changed_paths": candidate["changed_paths"],
        "check_changes": check_changes,
        "evidence": checked["reference"],
        "trust": "local-unattested",
    }


def assess_gate(root, id, resuming_to=None):
    root = Path(root).resolve()
    mission = load_mission(root, id)
    with Assessment(root, mission) as assessment:
        result = _assess_gate_snapshot(root, mission, assessment.candidate, resuming_to)
        try:
            assessment.finish()
        except FactoryError as exc:
            result["reasons"].append(str(exc))
            result["pass"] = False
        return result


def _full_ref(root, name):
    if not name or name.startswith("-"):
        return None
    try:
        return git(root, "rev-parse", "--verify", "--quiet", "--symbolic-full-name", name) or None
    except FactoryError:
        return None


def resolve_recorded_trunk(root, ref):
    if not isinstance(ref, str) or not re.match(r"^refs/(heads|remotes)/", ref):
        raise FactoryError(f"Recorded trunk {ref} is not a branch ref")
    return {
        "name": ref,
        "sha": git(root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"),
    }


def select_trunk(root, explicit=None):
    remotes = git(root, "remote").splitlines()
    if remotes:
        ref = _full_ref(root, explicit) if explicit else None
        if not explicit:
            try:
                ref = git(root, "symbolic-ref", "refs/remotes/origin/HEAD")
            except FactoryError:
                pass
        if not ref or not ref.startswith("refs/remotes/"):
            raise FactoryError(
                "This repository has remotes; trunk must be a remote-tracking ref (pass --trunk origin/BRANCH)"
            )
    else:
        ref = next(
            (
                ref
                for name in ([explicit] if explicit else ["main", "master"])
                if (ref := _full_ref(root, name))
            ),
            None,
        )
        if not ref or not ref.startswith("refs/heads/"):
            raise FactoryError("Cannot resolve local trunk; pass --trunk BRANCH")
    return {
        **resolve_recorded_trunk(root, ref),
        "kind": "remote" if remotes else "local",
    }


def is_ancestor(root, ancestor, descendant):
    try:
        git(root, "merge-base", "--is-ancestor", ancestor, descendant)
        return True
    except FactoryError:
        return False


def same_branch(branch, trunk):
    short = re.sub(r"^refs/heads/|^refs/remotes/[^/]+/", "", trunk or "")
    return branch == trunk or branch == short


def record_ci(root, id, url=None, head=None, conclusion=None, reason=None, trunk=None):
    url = require_text(url, "CI URL")
    if not isinstance(head, str) or not re.fullmatch("[a-f0-9]{40,64}", head):
        raise FactoryError("CI head must be the full candidate commit SHA")
    conclusion = require_text(conclusion, "CI conclusion")
    if conclusion != "success":
        reason = require_text(reason, "Failed CI reason")

        def failed(mission):
            if mission["state"] != "READY_PR":
                raise FactoryError("A CI result can only be recorded for READY_PR")
            mission.setdefault("ci_failures", []).append(
                {
                    "url": url,
                    "head_sha": head,
                    "conclusion": conclusion,
                    "reason": reason,
                    "at": now(),
                }
            )
            mission.setdefault("delivery", {}).pop("ci_ref", None)
            mission["state"], mission["previous_state"] = "IMPLEMENTING", None

        return update_mission(root, id, failed)
    gate = assess_gate(root, id)
    if not gate["pass"]:
        raise FactoryError("CI success requires a passing local gate: " + "; ".join(gate["reasons"]))
    snapshot = candidate_snapshot(root)
    if snapshot["dirty_paths"]:
        raise FactoryError(
            "Commit the reviewed candidate before recording CI: " + ", ".join(snapshot["dirty_paths"])
        )
    if head != snapshot["head"]:
        raise FactoryError("CI head is not the reviewed candidate commit")
    selected = select_trunk(root, trunk)
    branch = git(root, "symbolic-ref", "--short", "HEAD")
    if same_branch(branch, selected["name"]) or is_ancestor(root, head, selected["sha"]):
        raise FactoryError("CI must be recorded from a work branch with candidate not yet on trunk")

    def mutate(mission):
        if mission["state"] != "READY_PR":
            raise FactoryError("A CI result can only be recorded for READY_PR")
        current = assess_gate(root, id)
        if (
            not current["pass"]
            or current["fingerprint"] != gate["fingerprint"]
            or current["evidence"] != gate["evidence"]
        ):
            raise FactoryError("Candidate or evidence changed while recording CI")
        mission.setdefault("delivery", {})["ci_ref"] = {
            "fingerprint_format": "git-mode-v1",
            "url": url,
            "head_sha": head,
            "branch": branch,
            "trunk": selected["name"],
            "trunk_kind": selected["kind"],
            "conclusion": "success",
            "fingerprint": gate["fingerprint"],
            "verification_ref": gate["evidence"],
            "verification_sha256": hash_file(root, gate["evidence"]),
            "recorded_at": now(),
        }

    return update_mission(root, id, mutate, readiness=True)


def assess_merged(root, id, mission=None):
    mission = mission or load_mission(root, id)
    delivery = mission.get("delivery", {})
    ci = delivery.get("ci_ref")
    reasons = []

    def commit_exists(commit):
        try:
            git(root, "cat-file", "-e", f"{commit}^{{commit}}")
            return True
        except FactoryError:
            return False

    merge = delivery.get("merge_ref")
    if not merge or not commit_exists(merge):
        reasons.append("delivery.merge_ref must name an actual existing merge commit")
    if not ci:
        reasons.append("A successful CI reference for the reviewed candidate commit is required")
    else:
        if ci["conclusion"] != "success" or not commit_exists(ci["head_sha"]):
            reasons.append("CI candidate commit is missing or CI did not succeed")
        if not any(
            d["kind"] == "merge" and d["subject_hash"] == ci["fingerprint"] and d["reference"].strip()
            for d in mission["decisions"]
        ):
            reasons.append("Merge requires an external decision reference bound to the reviewed fingerprint")
        accepted = ci.get("verification_ref")
        try:
            if (
                not accepted
                or accepted not in mission["evidence"]
                or hash_file(root, accepted) != ci.get("verification_sha256")
            ):
                raise FactoryError("Accepted verification changed since CI recording")
            config = validate(
                root,
                "factory",
                json.loads(git(root, "show", f"{ci['head_sha']}:factory.json")),
            )
            candidate = {
                "fingerprint": ci["fingerprint"],
                "fingerprint_format": ci.get("fingerprint_format"),
                "head": ci["head_sha"],
                "spec_hash": mission["spec_hash"],
            }
            current = validate_verification(root, mission, config, candidate)
            reasons += current["reasons"]
            if current["reference"] != accepted:
                reasons += validate_verification(
                    root,
                    mission,
                    config,
                    candidate,
                    reference=accepted,
                    check_logs=False,
                )["reasons"]
            for task in mission["tasks"]:
                if task["status"] != "DONE":
                    reasons.append(f"Task {task['id']} is not DONE")
                try:
                    validate_completed_result(root, mission, task, ci["fingerprint"], accepted)
                except (FactoryError, OSError) as exc:
                    reasons.append(f"Task {task['id']} result invalid for CI candidate: {exc}")
        except (FactoryError, OSError, ValueError) as exc:
            reasons.append(f"Invalid CI candidate verification: {exc}")
        reasons += review_reasons(
            mission, ci["fingerprint"], "Latest independent review does not pass the CI candidate"
        )
        if not ci.get("branch") or not ci.get("trunk") or same_branch(ci["branch"], ci["trunk"]):
            reasons.append("CI result must come from a work branch other than trunk")
        try:
            trunk = resolve_recorded_trunk(root, ci.get("trunk"))
            if merge and commit_exists(merge):
                if not is_ancestor(root, merge, trunk["sha"]):
                    reasons.append(
                        "Merge commit is not reachable from the recorded trunk; nothing was integrated"
                    )
                if is_ancestor(root, merge, mission["base_commit"]):
                    reasons.append("Merge commit predates the mission base commit")
        except FactoryError as exc:
            reasons.append(str(exc))
        if merge and not reasons:
            changed = [
                p
                for p in git(
                    root,
                    "diff",
                    "--no-ext-diff",
                    "--name-only",
                    "-z",
                    "--no-renames",
                    mission["base_commit"],
                    ci["head_sha"],
                    "--",
                ).split("\0")
                if p and not is_metadata(p)
            ]
            if changed:
                missing = list(
                    filter(
                        None,
                        git(
                            root,
                            "diff",
                            "--no-ext-diff",
                            "--name-only",
                            "-z",
                            "--no-renames",
                            ci["head_sha"],
                            merge,
                            "--",
                            *[f":(literal){p}" for p in changed],
                        ).split("\0"),
                    )
                )
                reasons += [f"Merge commit does not contain reviewed candidate content: {p}" for p in missing]
    return {
        "pass": not reasons,
        "reasons": reasons,
        "mode": "merged",
        "fingerprint": ci.get("fingerprint") if ci else None,
        "ci_head": ci.get("head_sha") if ci else None,
        "merge_ref": merge,
        "trust": "local-unattested",
    }


def assess_delivery(root, id, mission=None, state=None):
    mission = mission or load_mission(root, id)
    result = assess_merged(root, id, mission)
    result["reasons"] += delivery_reasons(mission, state or effective_state(mission))
    result["pass"] = not result["reasons"]
    return result


def assess_current(root, id):
    mission = load_mission(root, id)
    return (
        assess_delivery(root, id, mission)
        if effective_state(mission) in POST_MERGE_STATES
        else assess_gate(root, id)
    )


def mission_status(root, id):
    mission = load_mission(root, id)
    if effective_state(mission) in POST_MERGE_STATES | {"READY_PR"}:
        try:
            result = assess_current(root, id)
            mission["live_gate"] = {
                "pass": result["pass"],
                "stale": not result["pass"],
                "mode": result.get("mode", "gate"),
                "fingerprint": result["fingerprint"],
                "reasons": result["reasons"],
            }
        except (FactoryError, OSError) as exc:
            mission["live_gate"] = {
                "pass": False,
                "stale": True,
                "mode": "error",
                "reasons": [str(exc)],
            }
    return mission


def list_missions(root):
    directory = safe_path(root, ".factory/missions")
    missions, terminal = [], 0
    for entry in directory.iterdir() if directory.exists() else []:
        if not entry.is_dir() or not (entry / "mission.json").exists():
            continue
        try:
            mission = load_mission(root, entry.name)
            if mission["state"] in TERMINAL_STATES:
                terminal += 1
                continue
            missions.append(
                {key: mission.get(key) for key in ("id", "title", "state", "previous_state", "updated_at")}
                | {"blockers": len(mission["blockers"])}
            )
        except FactoryError as exc:
            missions.append({"id": entry.name, "error": str(exc)})
    missions.sort(key=lambda m: m.get("updated_at", ""), reverse=True)
    return {"missions": missions, "terminal": terminal}


def create_packet(root, id, kind="pr"):
    if kind not in {"pr", "handoff", "release", "recovery"}:
        raise FactoryError("Packet kind must be pr, handoff, release, or recovery")
    mission = load_mission(root, id)
    assessment = Assessment(root, mission) if kind == "pr" else None
    try:
        try:
            gate = (
                _assess_gate_snapshot(root, mission, assessment.candidate)
                if assessment
                else assess_current(root, id)
            )
        except (FactoryError, OSError) as exc:
            if kind == "pr":
                raise
            gate = {
                "pass": False,
                "fingerprint": None,
                "reasons": [f"Gate could not run: {exc}"],
                "changed_paths": [],
            }
        if kind == "pr" and not gate["pass"]:
            raise FactoryError("Cannot prepare ready PR packet: " + "; ".join(gate["reasons"]))
        line = lambda value: str(value).replace("\r", " ").replace("\n", " ").replace("`", "\\`")
        content = [
            f"# {kind.title()} packet: {line(mission['title'])}",
            "",
            f"Mission: {id}",
            f"Recorded state: {mission['state']}",
            f"Candidate fingerprint: {gate['fingerprint']}",
            "",
            "This packet records local evidence. It does not establish human approval, external CI success, merge, deployment, or recovery.",
            "",
        ]
        if kind in {"release", "recovery"}:
            if not load_config(root)["delivery"]["enabled"]:
                raise FactoryError("Delivery is disabled; configure and validate the process first")
            required = (
                ("artifact_digest", "staging_ref")
                if kind == "release"
                else ("artifact_digest", "recovery_ref")
            )
            missing = [key for key in required if not mission.get("delivery", {}).get(key)]
            if missing:
                raise FactoryError(f"Cannot prepare {kind} packet: missing " + ", ".join(missing))
            content += (
                ["## External references", ""]
                + [f"- {key}: {line(value)}" for key, value in mission["delivery"].items()]
                + [
                    "",
                    "These supplied references are not authenticated and no deployment command was executed.",
                    "",
                ]
            )
        if kind == "handoff":
            content += [
                "## Workspace",
                "",
                f"HEAD: {git(root, 'rev-parse', 'HEAD')}",
                "",
                "```text",
                git(root, "status", "--short", "--untracked-files=all") or "clean",
                "```",
                "",
                "## Blockers and suspended work",
                "",
                *[f"- {line(b)}" for b in mission["blockers"]],
                *[f"- Suspended: {line(s)}" for s in mission.get("suspended_tasks", [])],
                "",
            ]
        content += [
            "## Scope",
            "",
            "See [accepted specification](spec.md) and [plan](plan.md).",
            "",
            "## Tasks",
            "",
            *[
                f"- {t['id']}: {line(t['title'])} — {t['status']}; attempts: {t['attempts']}"
                for t in mission["tasks"]
            ],
            "",
            "## Changed files",
            "",
            *[f"- `{line(p)}`" for p in gate.get("changed_paths", [])],
            "",
            *(
                ["## Check definition changes", "", *[f"- {line(c)}" for c in gate["check_changes"]], ""]
                if gate.get("check_changes")
                else []
            ),
            "## Verification",
            "",
            "Local gate: " + ("PASS" if gate["pass"] else "NOT READY"),
            *[f"- {line(r)}" for r in gate["reasons"]],
            "",
        ]
        if gate.get("evidence"):
            evidence = read_json(root, gate["evidence"])
            content += [
                f"- {line(c['id'])}: {c['status']}; exit {c['exit_code']}; {c['duration_ms']} ms"
                for c in evidence["checks"]
            ] + [""]
        if mission["reviews"]:
            review = mission["reviews"][-1]
            content += [
                "## Review",
                "",
                f"{line(review['id'])} by {line(review['author'])}: {review['status']} for {review['fingerprint']}",
                *[
                    f"- Resolved {line(r['finding'])}: {line(r['reason'])}"
                    for r in review.get("resolutions", [])
                ],
                "",
            ]
        if kind == "pr":
            risks = pr_prerequisites(root, id)["risks"]
            content += ["## Risks", "", risks["body"], ""]
        content += [
            "## Decision references",
            "",
            *[
                f"- {d['kind']}: {line(d['reference'])} (subject {d['subject_hash']})"
                for d in mission["decisions"]
            ],
            "",
            "## Remaining action",
            "",
            "Continue from the recorded state. External CI, merge, and deployment require their actual evidence.",
            "",
            "## Recovery",
            "",
            "See [recovery plan](recovery.md).",
            "",
        ]
        relative = f".factory/missions/{id}/" + ("pull-request.md" if kind == "pr" else f"{kind}-packet.md")
        if assessment:
            assessment.finish()
        write_bytes(root, relative, "\n".join(content).encode())
        if assessment:
            try:
                assessment.assert_current()
            except BaseException:
                safe_path(root, relative).unlink(missing_ok=True)
                raise
        return {
            "path": relative,
            "pass": gate["pass"],
            "fingerprint": gate["fingerprint"],
            **({"executed": False, "references_verified": False} if kind in {"release", "recovery"} else {}),
        }
    finally:
        if assessment:
            assessment.close()


def _input(args):
    if not getattr(args, "input", None):
        raise FactoryError("--input requires a JSON file path")
    # Like models --input: a root-relative path read without following symlinks.
    return read_json(args.root, args.input)


def _mission_handler(args):
    root, command = args.root, args.mission_command
    id = getattr(args, "mission", None)
    catalog = read_json(root, args.model_catalog) if getattr(args, "model_catalog", None) else None
    if command == "create":
        return create_mission(
            root,
            _input(args)
            if args.input
            else {
                "id": args.id,
                "title": args.title,
                "kind": args.kind,
                "base": args.base,
            },
        )
    if command == "list":
        return list_missions(root)
    if command == "recover-lock":
        return recover_lock(root)
    if command == "status":
        return mission_status(root, id)
    if command == "task-add":
        return add_task(root, id, _input(args))
    if command == "task-update":
        return edit_task(root, id, args.task, _input(args))
    if command == "task-transition":
        return transition_task(root, id, args.task, args.to, catalog)
    if command in {"transition", "block"}:
        return transition_mission(
            root,
            id,
            args.to if command == "transition" else "BLOCKED",
            args.reason,
            args.next,
            getattr(args, "decision", None),
        )
    if command == "resume":
        return resume_mission(root, id, args.to, args.resolution, catalog, args.replan_models)
    if command == "accept-scope":
        return accept_scope(root, id)
    if command == "decision":
        return record_decision(root, id, _input(args))
    if command == "review":
        return record_review(root, id, _input(args))
    if command == "record-result":
        return record_result(root, id, _input(args))
    if command == "record-results":
        return record_results(root, id, _input(args))
    if command == "model-plan":
        return register_model_plan(root, id, _input(args))
    if command == "record-delivery":
        return record_delivery(root, id, _input(args))
    if command == "ci-result":
        return record_ci(root, id, args.url, args.head, args.conclusion, args.reason, args.trunk)
    raise FactoryError(f"Unknown mission command: {command}")


def add_parser(subparsers):
    from .checks import run_checks, verify_mission

    mission = subparsers.add_parser("mission", help="Manage recoverable mission records")
    actions = mission.add_subparsers(dest="mission_command", required=True, metavar="<action>")
    summaries = {
        "create": "Create a mission from --input JSON or --id/--title/--kind/--base",
        "list": "List missions and their states",
        "status": "Show one mission's state, tasks and next action",
        "recover-lock": "Remove a stale state lock whose owner process has exited",
        "task-add": "Add a task from --input JSON",
        "task-update": "Update a task from --input JSON",
        "task-transition": "Move a task to another state (--to)",
        "transition": "Move the mission to another state (--to)",
        "block": "Block the mission with a reason and next step",
        "resume": "Resume a blocked mission with a recorded resolution",
        "accept-scope": "Record acceptance of the mission scope",
        "decision": "Record a decision from --input JSON",
        "review": "Record a review from --input JSON",
        "record-result": "Record one task result from --input JSON",
        "record-results": "Record several task results from --input JSON",
        "model-plan": "Register a validated model plan from --input",
        "ci-result": "Record an external CI result for a revision",
        "record-delivery": "Record delivery evidence from --input JSON",
    }
    options = {
        "mission": ("ID", "Mission ID"),
        "id": ("ID", "New mission ID"),
        "title": ("TEXT", "Mission title"),
        "kind": ("KIND", "Mission work type"),
        "base": ("REV", "Baseline Git revision"),
        "input": ("PATH", "Repository path of the input JSON"),
        "task": ("ID", "Task ID"),
        "to": ("STATE", "Target state"),
        "model-catalog": ("PATH", "Current model catalog JSON for model-bound tasks"),
        "reason": ("TEXT", "Reason recorded with the change"),
        "next": ("TEXT", "Next step recorded with the change"),
        "decision": ("TEXT", "Decline decision reference when canceling"),
        "resolution": ("TEXT", "How the blocking issue was resolved"),
        "url": ("URL", "CI run URL"),
        "head": ("SHA", "Full candidate commit SHA the CI run tested"),
        "conclusion": ("RESULT", "CI conclusion, such as success"),
        "trunk": ("BRANCH", "Trunk branch (default: detected)"),
    }

    def option(parser, name, **kwargs):
        metavar, summary = options[name]
        parser.add_argument("--" + name, metavar=metavar, help=summary, **kwargs)

    for command in (
        "create",
        "list",
        "status",
        "recover-lock",
        "task-add",
        "task-update",
        "task-transition",
        "transition",
        "block",
        "resume",
        "accept-scope",
        "decision",
        "review",
        "record-result",
        "record-results",
        "model-plan",
        "ci-result",
        "record-delivery",
    ):
        parser = actions.add_parser(command, help=summaries[command], description=summaries[command])
        parser.set_defaults(handler=_mission_handler)
        if command not in {"create", "list", "recover-lock"}:
            option(parser, "mission", required=True)
        if command == "create":
            for name in ("id", "title", "kind", "base", "input"):
                option(parser, name)
        if command in {
            "task-add",
            "task-update",
            "decision",
            "review",
            "record-result",
            "record-results",
            "model-plan",
            "record-delivery",
        }:
            option(parser, "input", required=True)
        if command in {"task-update", "task-transition"}:
            option(parser, "task", required=True)
        if command in {"task-transition", "transition", "resume"}:
            option(parser, "to", required=True)
        if command in {"task-transition", "resume"}:
            option(parser, "model-catalog")
        if command in {"transition", "block", "ci-result"}:
            option(parser, "reason")
        if command in {"transition", "block"}:
            option(parser, "next")
        if command == "transition":
            option(parser, "decision")
        if command == "resume":
            option(parser, "resolution", required=True)
            parser.add_argument(
                "--replan-models",
                action="store_true",
                help="Resume a paused bound task to IMPLEMENTING for model replanning",
            )
        if command == "ci-result":
            for name in ("url", "head", "conclusion"):
                option(parser, name, required=True)
            option(parser, "trunk")
    status = subparsers.add_parser("status", help="Show mission status without starting work")
    status.add_argument("--mission", metavar="ID", help="Mission ID (default: list all missions)")
    status.set_defaults(
        handler=lambda a: mission_status(a.root, a.mission) if a.mission else list_missions(a.root)
    )
    checks = subparsers.add_parser("checks", help="Run configured setup and product checks")
    checks.add_argument("--only", metavar="ID", help="Run only this configured check")
    checks.add_argument("--require-clean", action="store_true", help="Fail unless the working tree is clean")
    checks.set_defaults(handler=lambda a: run_checks(a.root, a.only, a.require_clean))
    verify = subparsers.add_parser("verify", help="Run checks and record mission verification evidence")
    verify.add_argument("--mission", required=True, metavar="ID", help="Mission ID")
    verify.add_argument("--revision", required=True, metavar="REV", help="Git revision to verify")
    verify.add_argument(
        "--candidate-root", metavar="PATH", help="Worktree of a recorded postmerge CI candidate"
    )
    verify.add_argument(
        "--reconcile-postmerge",
        action="store_true",
        help="Reconcile a held postmerge mission (needs --resolution)",
    )
    verify.add_argument("--resolution", metavar="TEXT", help="Resolution text for --reconcile-postmerge")
    verify.set_defaults(
        handler=lambda a: verify_mission(
            a.root,
            a.mission,
            a.revision,
            a.candidate_root,
            a.reconcile_postmerge,
            a.resolution,
        )
    )
    gate = subparsers.add_parser("gate", help="Assess readiness without changing mission state")
    gate.add_argument("--mission", required=True, metavar="ID", help="Mission ID")
    gate.set_defaults(handler=lambda a: assess_current(a.root, a.mission))
    packet = subparsers.add_parser("packet", help="Prepare local PR, handoff, release, or recovery packets")
    packet.add_argument("--mission", required=True, metavar="ID", help="Mission ID")
    packet.add_argument(
        "--kind",
        choices=("pr", "handoff", "release", "recovery"),
        default="pr",
        help="Packet type: pr, handoff, release or recovery (default: pr)",
    )
    packet.set_defaults(handler=lambda a: create_packet(a.root, a.mission, a.kind))
