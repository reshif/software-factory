"""Explicit JEV selection of eligible native models, with offline plan replay.

Only route_model_plan performs remote selection. Saved Choice observations are
local, editable evidence; they do not authenticate a provider or execute a model.

A routing record's ``provider_requests`` counts the assignments whose receipt
holds a JEV response: one logical Choice request each. HTTP retries inside one
request are counted by that receipt's ``attempts`` (1 to 3), not here.
"""

from __future__ import annotations

import copy
import json
import time
from pathlib import Path

from . import auth
from .core import FactoryError, asset_path, canonical, digest, load_config, now, read_json, safe_path, sha256
from .jev import MAX_RETRIES, JevError, request_jev, validate_jev_response
from .redaction import redact

MAX_ASSIGNMENTS = 16
MAX_CANDIDATES = 254
# A plan's sequential JEV requests share one budget: deadline_ms per assignment,
# never above this. Each request gets min(deadline_ms, remaining plan budget).
MAX_PLAN_DEADLINE_MS = 120000
RUBRIC_PATH = "models/selection-rubric.json"

DEFAULTS = {
    "enabled": False,
    "provider": "typesafe",
    "model": "jev-1.13.0",
    "deadline_ms": 3000,
    "max_request_bytes": 24576,
    "max_response_bytes": 65536,
    # TypeSafe confidence-routing guidance: below this Choice confidence the
    # assignment stays unresolved for a human instead of taking the choice.
    "min_confidence": 0.6,
}
JEV_REASONS = ("jev_choice", "abstained", "low_confidence")
# Receipt fields that exist only when a JEV request was made.
JEV_RECEIPT_FIELDS = ("confidence", "threshold", "attempts", "masks")


def settings(config):
    values = config.get("jev", {})
    return {key: values.get(key, default) for key, default in DEFAULTS.items()}


def routing_configuration(config):
    """Only configuration that can change a routed plan or its later use.

    Semantic claim settings (claim_mode, claim_accept_confidence, max_pairs,
    cache, source age) and unrelated factory limits/checks are excluded so editing them does not
    invalidate saved model plans.
    """
    return {
        "jev": settings(config),
        "profile": config.get("profile"),
        "model_selection": config.get("model_selection"),
    }


def _rubric(root):
    try:
        value = json.loads(asset_path(root, RUBRIC_PATH).read_text())
        if (
            set(value) != {"id", "instructions", "abstain"}
            or value["id"] != "factory-model-routing-v1"
            or any(not isinstance(value[k], str) or not value[k].strip() for k in ("instructions", "abstain"))
        ):
            raise ValueError()
        return value
    except (OSError, ValueError, TypeError):
        raise FactoryError("Invalid model selection rubric") from None


def _context(root, request, catalog):
    config_path = safe_path(root, "factory.json")
    config = load_config(root) if config_path.is_file() else {}
    # The factory schema admits only the typesafe provider.
    selected = settings(config)
    paths = {
        "policy_hash": asset_path(root, "models/policy.json"),
        "recommendations_hash": asset_path(root, "models/recommendations.json"),
        "models_schema_hash": asset_path(root, "schemas/models.schema.json"),
        "factory_schema_hash": asset_path(root, "schemas/factory.schema.json"),
        "rubric_hash": asset_path(root, RUBRIC_PATH),
    }
    modules = [Path(__file__).parent / name for name in ("routing.py", "models.py", "jev.py", "core.py")]
    _rubric(root)
    contract = {
        "configuration_hash": digest(routing_configuration(config)) if config_path.is_file() else None,
        **{key: sha256(path.read_bytes()) for key, path in paths.items()},
        "implementation_hash": digest({path.name: sha256(path.read_bytes()) for path in modules}),
        "request_hash": digest(request),
        "catalog_hash": digest(catalog),
        "settings": selected,
    }
    # factory.json stays in the in-flight stamps so any edit, even a transient
    # toggle, aborts routing; saved plans only bind routing_configuration().
    watched = list(paths.values()) + modules + ([config_path] if config_path.is_file() else [])
    stamps = [(s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns) for s in (p.stat() for p in watched)]
    return contract, stamps


def attach_factory_routing(root, plan):
    """Attach explicit deterministic provenance without running model selection."""
    context, _ = _context(root, plan["request"], plan["catalog"])
    record = {
        "schema_version": 1,
        "engine": "factory-models",
        "contract": context,
        "decisions": [],
        "trust": "local-unattested",
        "native_model_applied": False,
        "provider_requests": 0,
    }
    record["record_hash"] = digest(record)
    plan["routing"] = record
    return plan


def _decision_body(root, request, assignment, requirements, eligible, model):
    """The JEV Choice request, its choice keys and the number of masks applied.

    The objective and guidance strengths are free text, so both pass through
    ``redact`` before they are sent; the receipt records the mask count.
    """
    rubric = _rubric(root)
    if len(eligible) > MAX_CANDIDATES:
        raise FactoryError(
            f"JEV routing supports at most {MAX_CANDIDATES} eligible choices per assignment", 2
        )
    choices = {f"m{i}": entry["model"]["id"] for i, entry in enumerate(eligible)}
    # The whole eligible entry stays bound by its digest in the receipt; JEV
    # receives only compact, decision-relevant metadata so more candidates fit.
    criteria = {
        key: f"Select {identifier} as the best documented fit." for key, identifier in choices.items()
    }
    criteria["abstain"] = rubric["abstain"]
    objective, masks = redact(request["objective"])
    candidates = []
    for key, entry in zip(choices, eligible, strict=True):
        candidate, count = _candidate(key, entry)
        candidates.append(candidate)
        masks += count
    body = {
        "model": model,
        "state": {
            "objective": objective,
            "assignment": {
                "id": assignment["id"],
                "role": assignment["role"],
                "intent": assignment["intent"],
                "requirements": requirements,
            },
            "candidates": candidates,
        },
        "questions": {
            "selection": {"type": "choice", "instructions": rubric["instructions"], "criteria": criteria}
        },
    }
    return body, choices, masks


CANDIDATE_FIELDS = (
    "id",
    "provider",
    "identity",
    "resolved_model",
    "lifecycle",
    "capabilities",
    "efforts",
    "default_effort",
    "context_tokens",
    "cost_tier",
)


def _candidate(key, entry):
    """Compact candidate text and its mask count: omits sources, model_ids, availability and nulls."""
    model = entry["model"]
    value = {"choice": key}
    value.update({name: model[name] for name in CANDIDATE_FIELDS if model.get(name) is not None})
    masks = 0
    if entry["guidance"]:
        value["strengths"], masks = redact(entry["guidance"]["strengths"])
    return value, masks


def _jev_outcome(response, choices, threshold):
    """Map a validated Choice answer to (selected model, reason) under the gate.

    ``threshold`` is None only for historical receipts made before the gate.
    """
    answer = response["answers"]["selection"]
    if answer["choice"] == "abstain":
        return None, "abstained"
    if threshold is not None and answer["confidence"] < threshold:
        return None, "low_confidence"
    return choices.get(answer["choice"]), "jev_choice"


def _automatic_decision(assignment, eligible):
    if assignment["preferred_model"]:
        selected = next((entry["model"]["id"] for entry in eligible if entry["preferred"]), None)
        return selected, "explicit_preference" if selected else "explicit_preference_unavailable"
    if not eligible:
        return None, "no_eligible_candidates"
    if len(eligible) == 1:
        # One hard-eligible candidate leaves JEV nothing to decide; never bill for it.
        return eligible[0]["model"]["id"], "single_eligible_candidate"
    return None


def route_model_plan(root, request, catalog, *, at=None, get_api_key=None, transport=None, input_paths=None):
    """Select one plan using the configured branch; ON errors never fall back."""
    from .models import create_model_plan, validate_document

    root = Path(root)
    original_request, original_catalog = request, catalog
    request, catalog = copy.deepcopy(request), copy.deepcopy(catalog)
    validate_document(root, "request", request)
    validate_document(root, "catalog", catalog)
    context, stamps = _context(root, request, catalog)
    selected = context["settings"]
    engine = "jev" if selected["enabled"] else "factory-models"
    if selected["enabled"] and len(request["assignments"]) > MAX_ASSIGNMENTS:
        raise FactoryError(f"JEV routing supports at most {MAX_ASSIGNMENTS} assignments per plan", 2)
    decisions = []
    plan_budget_ms = min(selected["deadline_ms"] * len(request["assignments"]), MAX_PLAN_DEADLINE_MS)
    deadline = time.monotonic() + plan_budget_ms / 1000
    credential = None

    def check_inputs():
        try:
            current, current_stamps = _context(root, original_request, original_catalog)
            if current != context or current_stamps != stamps:
                raise ValueError()
            for name in ("request", "catalog"):
                path = (input_paths or {}).get(name)
                if path and digest(read_json(root, path)) != context[f"{name}_hash"]:
                    raise ValueError()
        except (FactoryError, OSError, ValueError):
            raise FactoryError(
                "Model routing inputs or configuration changed; regenerate the plan", 2
            ) from None

    def select(assignment, requirements, eligible):
        nonlocal credential
        check_inputs()
        automatic = _automatic_decision(assignment, eligible)
        receipt = {
            "assignment_id": assignment["id"],
            "eligible_hash": digest(eligible),
            "request_hash": None,
            "response": None,
            "selected_model": None,
            "reason": "no_eligible_candidates",
        }
        if automatic is not None:
            receipt["selected_model"], receipt["reason"] = automatic
        else:
            body, choices, masks = _decision_body(
                root, request, assignment, requirements, eligible, selected["model"]
            )
            if len(canonical(body)) > selected["max_request_bytes"]:
                raise FactoryError(
                    "JEV model routing request exceeds the byte limit; no fallback was applied", 2
                )
            started = time.monotonic()
            remaining_ms = min(selected["deadline_ms"], int((deadline - started) * 1000))
            if remaining_ms <= 0:
                raise FactoryError("JEV model routing deadline exceeded; no fallback was applied", 2)
            if credential is None:
                credential, credential_reason = auth.request_credential(get_api_key)
            if credential is None:
                raise FactoryError(
                    f"JEV model routing {credential_reason}; run software-factory auth status or "
                    "software-factory auth login, or supply TYPESAFE_API_KEY",
                    2,
                )
            check_inputs()
            attempts = []
            try:
                response = (transport or request_jev)(
                    body,
                    api_key=credential,
                    deadline_ms=remaining_ms,
                    max_response_bytes=selected["max_response_bytes"],
                    on_attempt=attempts.append,
                )
                response = validate_jev_response(
                    response,
                    selected["model"],
                    ["selection"],
                    choices={"selection": body["questions"]["selection"]["criteria"]},
                )
            except JevError as exc:
                raise FactoryError(f"JEV model routing {exc.code}; no fallback was applied", 2) from None
            except Exception:  # noqa: BLE001 - never expose provider bodies or credentials
                raise FactoryError("JEV model routing provider failure; no fallback was applied", 2) from None
            # A response that arrives is kept even when it used the whole budget: it
            # is already billed, and the transport enforced remaining_ms itself.
            check_inputs()
            # request_jev reports attempts 1, 2, ...; a transport that reports
            # nothing made one call. Anything else is not a faithful count.
            count = len(attempts) or 1
            if (attempts and attempts != list(range(1, count + 1))) or count > MAX_RETRIES + 1:
                raise FactoryError(
                    "JEV model routing transport reported inconsistent attempts; no fallback was applied", 2
                )
            selected_model, reason = _jev_outcome(response, choices, selected["min_confidence"])
            receipt.update(
                request_hash=digest(body),
                response=response,
                selected_model=selected_model,
                reason=reason,
                confidence=response["answers"]["selection"]["confidence"],
                threshold=selected["min_confidence"],
                attempts=count,
                masks=masks,
            )
        decisions.append(receipt)
        return receipt["selected_model"]

    check_inputs()
    plan = create_model_plan(
        root, request, catalog, at=at or now(), selector=select if selected["enabled"] else None
    )
    check_inputs()
    record = {
        "schema_version": 1,
        "engine": engine,
        "contract": context,
        "decisions": decisions,
        "trust": "local-unattested",
        "native_model_applied": False,
        "provider_requests": sum(r["response"] is not None for r in decisions),
    }
    record["record_hash"] = digest(record)
    plan["routing"] = record
    return validate_document(root, "plan", plan)


def validate_recorded_routing(plan):
    """Check a historical receipt without reinterpreting it under today's controls."""
    record = plan["routing"]
    body = {key: value for key, value in record.items() if key != "record_hash"}
    if digest(body) != record["record_hash"]:
        raise FactoryError("Model routing record hash changed; regenerate the plan")
    if record["contract"]["request_hash"] != digest(plan["request"]) or record["contract"][
        "catalog_hash"
    ] != digest(plan["catalog"]):
        raise FactoryError("Model routing inputs differ from the recorded contract")
    expected_engine = "jev" if record["contract"]["settings"]["enabled"] else "factory-models"
    if record["engine"] != expected_engine:
        raise FactoryError("Model routing engine differs from its recorded JEV toggle")
    if record["provider_requests"] != sum(r["response"] is not None for r in record["decisions"]):
        raise FactoryError("Model routing provider request count differs from receipts")
    if record["engine"] == "factory-models":
        if record["decisions"]:
            raise FactoryError("Factory-models plan cannot contain JEV decisions")
        return
    assignments = {item["id"]: item for item in plan["assignments"]}
    if len(record["decisions"]) != len(assignments) or {
        r["assignment_id"] for r in record["decisions"]
    } != set(assignments):
        raise FactoryError("Model routing decisions do not match plan assignments")
    for receipt in record["decisions"]:
        item = assignments[receipt["assignment_id"]]
        selected_model = item["model"]["id"] if item["model"] else None
        if receipt["selected_model"] != selected_model or (item["status"] == "selected") != (
            selected_model is not None
        ):
            raise FactoryError("Model routing choice differs from its assignment")
        if receipt["reason"] in JEV_REASONS:
            candidates = sorted(item["fallbacks"] + ([selected_model] if selected_model else []))
            choices = {f"m{i}": identifier for i, identifier in enumerate(candidates)}
            if receipt["request_hash"] is None:
                raise FactoryError("Recorded JEV request hash is missing")
            try:
                response = validate_jev_response(
                    receipt["response"],
                    record["contract"]["settings"]["model"],
                    ["selection"],
                    choices={"selection": [*choices, "abstain"]},
                )
            except JevError:
                raise FactoryError("Invalid recorded JEV model routing response") from None
            threshold = _recorded_threshold(record, receipt, response)
            if response != receipt["response"] or _jev_outcome(response, choices, threshold) != (
                selected_model,
                receipt["reason"],
            ):
                raise FactoryError("Model routing choice differs from its recorded response")
        elif (
            receipt["request_hash"] is not None
            or receipt["response"] is not None
            or set(JEV_RECEIPT_FIELDS) & set(receipt)
        ):
            raise FactoryError("Explicit or unavailable assignment cannot invent a JEV request")


def _recorded_threshold(record, receipt, response):
    """The confidence floor a receipt was decided under, from its own contract.

    Plans recorded before the gate have neither a contract threshold nor receipt
    gate fields; they keep their recorded meaning. Newer receipts must restate
    the contract threshold and the response confidence exactly.
    """
    threshold = record["contract"]["settings"].get("min_confidence")
    fields = {"confidence", "threshold", "attempts"}
    if threshold is None:
        if fields & set(receipt):
            raise FactoryError("Model routing receipt records a threshold its contract lacks")
        return None
    if (
        not fields <= set(receipt)
        or receipt["threshold"] != threshold
        or receipt["confidence"] != response["answers"]["selection"]["confidence"]
    ):
        raise FactoryError("Model routing confidence gate differs from its recorded contract")
    return threshold


def replay_model_plan(root, plan):
    """Rebuild a saved routing decision with current controls, without I/O to a provider."""
    from .models import create_model_plan

    record = plan["routing"]
    validate_recorded_routing(plan)
    context, _ = _context(root, plan["request"], plan["catalog"])
    if context != record["contract"]:
        raise FactoryError(
            "Model routing configuration, rubric, schema or inputs changed; regenerate the plan"
        )
    expected_engine = "jev" if context["settings"]["enabled"] else "factory-models"
    if record["engine"] != expected_engine:
        raise FactoryError("Model routing engine differs from the current JEV toggle")
    if len(record["decisions"]) > MAX_ASSIGNMENTS:
        raise FactoryError("Model routing assignment limit exceeded")
    cursor = 0

    def select(assignment, requirements, eligible):
        nonlocal cursor
        if cursor >= len(record["decisions"]):
            raise FactoryError("Model routing decision missing")
        receipt = record["decisions"][cursor]
        cursor += 1
        if receipt["assignment_id"] != assignment["id"] or receipt["eligible_hash"] != digest(eligible):
            raise FactoryError("Model routing eligible candidates changed")
        automatic = _automatic_decision(assignment, eligible)
        if automatic is not None:
            selected_model, reason = automatic
            if receipt["request_hash"] is not None or receipt["response"] is not None:
                raise FactoryError("Explicit or unavailable assignment cannot invent a JEV request")
        else:
            request_body, choices, masks = _decision_body(
                root, plan["request"], assignment, requirements, eligible, context["settings"]["model"]
            )
            if receipt["request_hash"] != digest(request_body) or receipt.get("masks") != masks:
                raise FactoryError("Model routing request changed")
            try:
                response = validate_jev_response(
                    receipt["response"],
                    context["settings"]["model"],
                    ["selection"],
                    choices={"selection": request_body["questions"]["selection"]["criteria"]},
                )
            except JevError:
                raise FactoryError("Invalid recorded JEV model routing response") from None
            if response != receipt["response"]:
                raise FactoryError("Unexpected recorded JEV response fields")
            threshold = _recorded_threshold(record, receipt, response)
            if threshold != context["settings"]["min_confidence"]:
                raise FactoryError("Model routing confidence threshold changed")
            selected_model, reason = _jev_outcome(response, choices, threshold)
        if receipt["selected_model"] != selected_model or receipt["reason"] != reason:
            raise FactoryError("Model routing choice differs from its recorded response")
        return selected_model

    expected = create_model_plan(
        root,
        plan["request"],
        plan["catalog"],
        at=plan["created_at"],
        selector=select if expected_engine == "jev" else None,
    )
    if cursor != len(record["decisions"]):
        raise FactoryError("Unexpected model routing decisions")
    expected["routing"] = copy.deepcopy(record)
    return expected
