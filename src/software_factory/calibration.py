"""Optional local model outcome observations; never inference or a model ranking."""

from __future__ import annotations

import json
import math
import os
import re
import statistics
import tempfile
import time

from .core import (
    FactoryError,
    git,
    now,
    private_dir,
    read_json,
    safe_path,
    sha256,
    validate,
)
from .models import (
    _timestamp,
    model_hash,
    resolve_assignment,
    validate_task_observation,
)

DIRECTORY = ".factory/local/models/outcomes"


def assert_private_directory(root, directory, *, create=True):
    private_dir(root, create=create)
    safe_path(root, directory)
    try:
        git(
            root,
            "check-ignore",
            "--quiet",
            "--no-index",
            "--",
            directory.rstrip("/") + "/",
        )
        if git(root, "ls-files", "--", directory):
            raise FactoryError("Tracked private records")
    except FactoryError as exc:
        raise FactoryError(
            f"Private records require an effectively ignored, untracked {directory}/ directory"
        ) from exc


def outcome_template():
    return {
        "schema_version": 1,
        "kind": "outcome-input",
        "id": "OUTCOME-001",
        "mission_id": "M-EXAMPLE",
        "task_id": "T-001",
        "task_class": "bounded-change",
        "comparison": {
            "prompt_hash": None,
            "tools_hash": None,
            "acceptance_hash": None,
        },
        "execution_context": {"client_version": None, "reference": None},
        "measurements": {
            "accepted": None,
            "wall_time_ms": None,
            "missed_defects": None,
            "usage": None,
        },
        "provenance": {"kind": "unknown", "reference": None},
        "observed_at": now(),
    }


def validate_outcome(root, value):
    validate(root, "model-outcome", value)
    for field in ("observed_at", "recorded_at"):
        if field not in value:
            continue
        try:
            timestamp = _timestamp(value[field])
        except FactoryError as exc:
            raise FactoryError(
                "Outcome time must be a real, non-future UTC timestamp with milliseconds"
            ) from exc
        if (
            not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", value[field])
            or timestamp > time.time() + 300
        ):
            raise FactoryError("Outcome time must be a real, non-future UTC timestamp with milliseconds")
    supplied = any(item is not None for item in value["measurements"].values())
    if supplied and (
        value["provenance"]["kind"] == "unknown" or not (value["provenance"]["reference"] or "").strip()
    ):
        raise FactoryError("Measured outcomes require provenance and a concrete evidence reference")
    if value["provenance"]["kind"] != "unknown" and not (value["provenance"]["reference"] or "").strip():
        raise FactoryError("Outcome provenance requires an evidence reference")
    if (
        value["execution_context"]["client_version"] is not None
        and not (value["execution_context"]["reference"] or "").strip()
    ):
        raise FactoryError("Observed client version requires an execution evidence reference")
    if value["kind"] == "outcome":
        result = value["result"]
        if result["identity_provenance"] == "unknown" and (
            result["observed_model"] is not None or result["observed_effort"] is not None
        ):
            raise FactoryError("Unknown runtime identity cannot attribute a model or effort")
        if result["identity_provenance"] != "unknown" and not (result["identity_reference"] or "").strip():
            raise FactoryError("Model attribution requires an evidence reference")
        prefix = f".factory/missions/{value['mission_id']}/results/"
        record = result["path"].startswith(prefix + "records/") and re.fullmatch(
            r"[A-Za-z0-9_-]+\.json", result["path"][len(prefix + "records/") :]
        )
        if not record:
            raise FactoryError("Outcome result path does not match its mission")
        body = {k: v for k, v in value.items() if k != "content_hash"}
        if model_hash(body) != value["content_hash"]:
            raise FactoryError("Outcome content hash changed")
    return value


def record_model_outcome(root, input):
    from .workflow import load_mission, load_result, result_index

    validate_outcome(root, input)
    if input["kind"] != "outcome-input":
        raise FactoryError("Recording requires outcome-input metadata")
    assert_private_directory(root, DIRECTORY)
    mission = load_mission(root, input["mission_id"])
    task = next((t for t in mission["tasks"] if t["id"] == input["task_id"]), None)
    if not task or not task["attempts"]:
        raise FactoryError("Outcome task needs an actual recorded execution attempt")
    index = result_index(root, input["mission_id"])
    entry = index["records"].get(input["task_id"])
    if not entry:
        raise FactoryError("Outcome task has no indexed result")
    name = "records/" + entry["file"]
    relative = f".factory/missions/{input['mission_id']}/results/{name}"
    result = validate(
        root, "result", load_result(root, input["mission_id"], input["task_id"], supplied_index=index)
    )
    data = safe_path(root, relative).read_bytes()
    if sha256(data) != entry["sha256"] or model_hash(json.loads(data)) != model_hash(result):
        raise FactoryError("Task result changed while recording the outcome; retry with the current result")
    if result["mission_id"] != input["mission_id"] or result["task_id"] != input["task_id"]:
        raise FactoryError("Outcome result belongs to a different task")
    if input["measurements"]["accepted"] is True and (result["status"] != "complete" or result["unresolved"]):
        raise FactoryError("Accepted outcome requires a completed result without unresolved issues")
    observation = result.get("model_observation")
    validate_task_observation(root, mission, task, observation)
    plan = (
        resolve_assignment(root, mission, task["model_assignment"])["plan"]
        if task.get("model_assignment")
        else None
    )
    attempt = result.get("execution_attempt")
    if attempt is None:
        raise FactoryError("Outcome result has no execution attempt; a new executed result is required")
    if observation and attempt != observation["attempt"]:
        raise FactoryError("Result execution attempt disagrees with its model observation")
    if attempt != task["attempts"]:
        raise FactoryError("Outcome result does not match the current task attempt")
    observed = observation or {}
    record = {
        **input,
        "kind": "outcome",
        "recorded_at": now(),
        "result": {
            "path": relative,
            "sha256": sha256(data),
            "fingerprint": result["fingerprint"],
            "status": result["status"],
            "attempt": attempt,
            "observed_model": observed.get("observed_model"),
            "observed_effort": observed.get("observed_effort"),
            "identity_provenance": observed.get("provenance", "unknown"),
            "identity_reference": observed.get("reference"),
            "profile": plan["request"]["profile"] if plan else None,
            "harness": plan["request"]["harness"] if plan else None,
            "catalog_client_version": plan["catalog"]["client_version"] if plan else None,
            "billing_context": plan["catalog"]["billing_context"] if plan else None,
        },
    }
    record["content_hash"] = model_hash(record)
    validate_outcome(root, record)
    destination = f"{DIRECTORY}/{input['id']}.json"
    target = safe_path(root, destination)
    target.parent.mkdir(parents=True, exist_ok=True)
    safe_path(root, destination)
    descriptor, temporary = tempfile.mkstemp(prefix=".outcome-", dir=target.parent)
    try:
        with os.fdopen(descriptor, "w") as stream:
            json.dump(record, stream, indent=2, ensure_ascii=False, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        safe_path(root, destination)
        os.link(temporary, target)
    finally:
        os.unlink(temporary)
    return {
        "recorded": destination,
        "content_hash": record["content_hash"],
        "model_attributed": record["result"]["observed_model"] is not None,
        "applied": False,
    }


def _summary(values):
    data = sorted(v for v in values if type(v) in (int, float) and math.isfinite(v))
    return {
        "samples": len(data),
        "min": data[0] if data else None,
        "median": statistics.median(data) if data else None,
        "max": data[-1] if data else None,
    }


def model_calibration(root):
    directory = safe_path(root, DIRECTORY)
    if not directory.exists():
        return {
            "records": 0,
            "cohorts": [],
            "unattributed": [],
            "invalid": [],
            "limitations": [
                "No local outcomes have been recorded. No model ranking or automatic selection is produced."
            ],
        }
    files = sorted(p.name for p in directory.iterdir() if p.name.endswith(".json"))
    if len(files) > 10000:
        raise FactoryError("Calibration exceeds 10000 records; archive old local outcomes before reporting")
    groups, invalid, unattributed, seen, records = {}, [], [], set(), 0
    for filename in files:
        try:
            record = validate_outcome(root, read_json(root, DIRECTORY + "/" + filename))
            if record["kind"] != "outcome" or filename != record["id"] + ".json":
                raise FactoryError("Outcome filename/record mismatch")
            execution = (
                record["mission_id"],
                record["task_id"],
                record["result"]["attempt"],
            )
            if execution in seen:
                raise FactoryError("Duplicate execution outcome")
            seen.add(execution)
            records += 1
            result = record["result"]
            if (
                not result["observed_model"]
                or record["execution_context"]["client_version"] in (None, "unknown")
                or any(v is None for v in record["comparison"].values())
            ):
                unattributed.append(
                    {
                        "id": record["id"],
                        "reason": "Effective model identity was not observed"
                        if not result["observed_model"]
                        else "Comparable prompt/tool/acceptance hashes or observed execution client version are incomplete",
                    }
                )
                continue
            contract = {
                "task_class": record["task_class"],
                **record["comparison"],
                "profile": result["profile"],
                "harness": result["harness"],
                "client_version": record["execution_context"]["client_version"],
                "billing_context": result["billing_context"],
                "measurement_provenance": record["provenance"]["kind"],
                "identity_provenance": result["identity_provenance"],
            }
            key = model_hash(contract)
            group = groups.setdefault(key, {"id": key, "contract": contract, "models": {}})
            model_key = (result["observed_model"], result["observed_effort"])
            group["models"].setdefault(model_key, []).append(record)
        except (FactoryError, OSError, ValueError) as exc:
            invalid.append({"file": filename, "reason": str(exc)})
    cohorts = []
    for key in sorted(groups):
        group, rows = groups[key], []
        for (model, effort), observations in sorted(
            group["models"].items(), key=lambda kv: (kv[0][0], kv[0][1] or "")
        ):
            measured = [r for r in observations if r["measurements"]["accepted"] is not None]
            accepted = sum(r["measurements"]["accepted"] is True for r in measured)
            units = sorted(
                {r["measurements"]["usage"]["unit"] for r in observations if r["measurements"]["usage"]}
            )
            rows.append(
                {
                    "model": model,
                    "effort": effort,
                    "samples": len(observations),
                    "accepted": {
                        "samples": len(measured),
                        "count": accepted,
                        "rate": accepted / len(measured) if measured else None,
                    },
                    "prior_task_attempts": _summary(
                        [max(0, r["result"]["attempt"] - 1) for r in observations]
                    ),
                    **{
                        metric: _summary([r["measurements"][metric] for r in observations])
                        for metric in ("wall_time_ms", "missed_defects")
                    },
                    "usage": [
                        {
                            "unit": unit,
                            **_summary(
                                [
                                    r["measurements"]["usage"]["amount"]
                                    for r in observations
                                    if r["measurements"]["usage"]
                                    and r["measurements"]["usage"]["unit"] == unit
                                ]
                            ),
                        }
                        for unit in units
                    ],
                    "outcome_ids": [r["id"] for r in observations],
                    "uncertainty": "Descriptive observations only; sample size, task selection and operator judgments do not establish causal model superiority.",
                }
            )
        cohorts.append({"id": key, "contract": group["contract"], "models": rows})
    return {
        "records": records,
        "cohorts": cohorts,
        "unattributed": unattributed,
        "invalid": invalid,
        "limitations": [
            "Local records are editable observations, not authenticated benchmark results or readiness evidence.",
            "Only matching task class, prompt, tool, acceptance, client, billing and provenance contracts share a cohort. Usage units are never converted or pooled. Prior task attempts may include other models and are not attributed to the current model.",
            "No ranking, minimum-sample guarantee, automatic model selection, inference or paid benchmarking is performed. Review source outcomes before using them in an explicit plan rationale.",
        ],
    }
