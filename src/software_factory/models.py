"""Native model eligibility, offline plan replay and metadata-only discovery.

The plan CLI calls the configured JEV selector when enabled. Native model
execution and client settings remain under the host agent controls.
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import selectors
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from jsonschema import Draft7Validator

from .core import (
    FactoryError,
    asset_path,
    canonical,
    digest,
    load_config,
    now,
    profiles,
    read_json,
    safe_path,
    sha256,
    write_json,
)

MODEL_HARNESSES = {
    "codex": ["codex-native"],
    "claude": ["claude-code-native"],
    "copilot": ["copilot-local", "copilot-agent-host"],
}
model_hash = digest


def _unique(values, label):
    if len(set(values)) != len(values):
        raise FactoryError(f"Duplicate {label}")


def _timestamp(value):
    try:
        if not isinstance(value, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{3})?Z", value
        ):
            raise ValueError()
        parsed = datetime.fromisoformat(value)
        return parsed.timestamp()
    except (ValueError, TypeError, OverflowError) as exc:
        raise FactoryError("Invalid model evidence timestamp") from exc


def validate_document(root, kind, value):
    schema = json.loads(asset_path(root, "schemas/models.schema.json").read_text())
    if kind not in schema["definitions"]:
        raise FactoryError(f"Unknown model document: {kind}")
    schema.pop("oneOf", None)
    schema["$ref"] = f"#/definitions/{kind}"
    errors = sorted(Draft7Validator(schema).iter_errors(value), key=lambda e: str(e.path))
    if errors:
        location = "/".join(str(part) for part in errors[0].absolute_path) or "<document>"
        raise FactoryError(f"Invalid model {kind} at {location}: {errors[0].message}")
    if value.get("profile") and value["harness"] not in MODEL_HARNESSES[value["profile"]]:
        raise FactoryError("Model profile/harness mismatch")
    for field in ("observed_at", "created_at", "checked_at"):
        if value.get(field):
            _timestamp(value[field])
    if kind == "catalog":
        _unique([m["id"] for m in value["models"]], "catalog model ID")
        if value["provenance"]["kind"] == "unavailable" and value["models"]:
            raise FactoryError("Unavailable inventory cannot advertise models")
        if value["parent_model"] is not None and not any(
            m["id"] == value["parent_model"] and m["availability"] == "visible" for m in value["models"]
        ):
            raise FactoryError("Observed parent must identify a visible catalog model")
        for m in value["models"]:
            if m["default_effort"] and m["default_effort"] not in m["efforts"]:
                raise FactoryError(f"Unsupported default effort: {m['id']}")
            if m["identity"] == "auto" and value["profile"] != "copilot":
                raise FactoryError("Auto strategy is only supported for Copilot")
            # Exact and auto entries name themselves; an alias must resolve elsewhere.
            if m["identity"] != "alias" and m["resolved_model"] is not None:
                raise FactoryError(
                    f"Only aliases record resolved_model; set it to null for {m['identity']} model {m['id']}"
                )
            if m["identity"] == "alias" and m["resolved_model"] == m["id"]:
                raise FactoryError(f"Alias {m['id']} cannot resolve to itself; record it as exact")
    if kind == "request":
        _unique([a["id"] for a in value["assignments"]], "assignment ID")
        _unique([a["key"] for a in value["research"]], "research guidance key")
        for entry in value["research"]:
            _timestamp(entry["checked_at"])
            _guidance_names(entry)
        if sum(a["requirements"]["operation"] == "main" for a in value["assignments"]) > 1:
            raise FactoryError("A plan has at most one main-session assignment")
        if any(a["preferred_model"] and not (a["rationale"] or "").strip() for a in value["assignments"]):
            raise FactoryError("An explicit model preference requires a rationale")
    if kind == "recommendations":
        _unique([e["key"] for e in value["entries"]], "guidance key")
        for entry in value["entries"]:
            _timestamp(entry["checked_at"])
            _guidance_names(entry)
    if kind == "observation":
        if _timestamp(value["observed_at"]) > time.time() + 300:
            raise FactoryError("Model observation time is in the future")
        if value["provenance"] == "unknown" and (
            value["observed_model"] is not None or value["observed_effort"] is not None
        ):
            raise FactoryError("Unknown observation cannot assert effective identity")
        if value["provenance"] != "unknown" and not (value["reference"] or "").strip():
            raise FactoryError("Model observations need an evidence reference")
    return value


def _guidance_names(entry):
    """Native IDs and client display names are disjoint lists of one guidance entry."""
    names = [n.lower() for n in entry["model_ids"] + entry.get("display_names", [])]
    if len(set(names)) != len(names):
        raise FactoryError(f"Guidance {entry['key']} repeats a model ID or display name")


def _identifiers(entry, profile):
    """Identifiers a catalog of ``profile`` may use for this guidance.

    Only Copilot selects models by display name; Codex and Claude need native IDs,
    so a display name there is never matched (and never dispatched).
    """
    return entry["model_ids"] + (entry.get("display_names", []) if profile == "copilot" else [])


# Research may refresh reviewed guidance evidence, never what the key identifies.
RESEARCH_REFRESHABLE = ("strengths", "sources", "checked_at")


def guidance_entries(request, recommendations):
    """Request research merged over reviewed guidance, keyed by guidance key.

    A research entry reusing a reviewed key may only refresh its strengths,
    sources and checked_at; re-binding a reviewed preference key to other models,
    providers, profiles or client prerequisites is rejected.
    """
    reviewed = {e["key"]: e for e in recommendations["entries"]}
    for entry in request["research"]:
        original = reviewed.get(entry["key"])
        if original is None:
            continue
        fixed = {k: v for k, v in entry.items() if k not in RESEARCH_REFRESHABLE}
        if fixed != {k: v for k, v in original.items() if k not in RESEARCH_REFRESHABLE}:
            changed = sorted(
                k
                for k in set(fixed) | set(original)
                if k not in RESEARCH_REFRESHABLE and fixed.get(k) != original.get(k)
            )
            raise FactoryError(
                f"Research entry {entry['key']} re-binds reviewed guidance ({', '.join(changed)}); "
                "only strengths, sources and checked_at may be refreshed. Use a new research key for "
                "other models"
            )
    keys = {r["key"] for r in request["research"]}
    return request["research"] + [e for e in recommendations["entries"] if e["key"] not in keys]


def model_controls(root):
    policy = validate_document(root, "policy", json.loads(asset_path(root, "models/policy.json").read_text()))
    recommendations = validate_document(
        root,
        "recommendations",
        json.loads(asset_path(root, "models/recommendations.json").read_text()),
    )
    keys = {e["key"] for e in recommendations["entries"]}
    for prefs in policy["preferences"].values():
        if any(key not in keys for key in prefs):
            raise FactoryError("Unknown guidance preference")
    return {
        "policy": policy,
        "recommendations": recommendations,
        "policy_hash": model_hash(policy),
        "recommendations_hash": model_hash(recommendations),
    }


def request_template(*, profile, harness, policy, objective, id="PLAN-001", session_id="SESSION-001"):
    return {
        "schema_version": 1,
        "kind": "request",
        "id": id,
        "objective": objective,
        "profile": profile,
        "harness": harness,
        "session_id": session_id,
        "research": [],
        "assignments": [
            {
                "id": role,
                "role": role,
                "intent": policy["role_intents"][role],
                "preferred_model": None,
                "rationale": None,
                "requirements": {
                    "operation": "main" if role == "orchestrator" else "subagent",
                    "capabilities": ["text", "tools"],
                    "min_context_tokens": None,
                    "allowed_providers": [],
                    "exact_model": None,
                    "effort": None,
                    "max_cost_tier": None,
                    "parent_cost_tier": None,
                    "allow_preview": False,
                },
            }
            for role in (
                "orchestrator",
                "planner",
                "implementer",
                "verifier",
                "reviewer",
            )
        ],
    }


def catalog_template(*, profile, harness, session_id="SESSION-001"):
    return {
        "schema_version": 1,
        "kind": "catalog",
        "profile": profile,
        "harness": harness,
        "session_id": session_id,
        "parent_model": None,
        "client_version": "unknown",
        "billing_context": "unknown",
        "observed_at": now(),
        "provenance": {
            "kind": "unavailable",
            "reference": "No account inventory observed. Inspect the actual client model picker.",
        },
        "models": [],
    }


def _identity_guidance(model, catalog, entries):
    identity = model["resolved_model"] if model["identity"] == "alias" else model["id"]
    return [
        e
        for e in entries
        if e["provider"] == model["provider"]
        and catalog["profile"] in e["profiles"]
        and identity in _identifiers(e, catalog["profile"])
    ]


def _guidance_for(model, catalog, entries):
    return next(
        (
            e
            for e in _identity_guidance(model, catalog, entries)
            if not model["guidance_key"] or e["key"] == model["guidance_key"]
        ),
        None,
    )


def _version_reasons(guidance, catalog):
    minimum = guidance.get("minimum_client_versions", {}).get(catalog["harness"])
    if not minimum:
        return []
    # "2.1.300 (Claude Code)", "v2.1.300" and "codex-cli 0.46.0" all name a version.
    match = re.search(r"(?<![\d.])(\d+)\.(\d+)\.(\d+)(?!\.?\d)", catalog["client_version"])
    if not match or tuple(map(int, match.groups())) < tuple(map(int, minimum.split("."))):
        return [f"Client version {minimum} or newer is required for this model"]
    return []


def eligibility(model, requirement, catalog, policy):
    reasons = []
    if model["availability"] != "visible":
        reasons.append(f"Availability is {model['availability']}")
    if requirement["operation"] not in model["operations"]:
        reasons.append(f"Availability for {requirement['operation']} is unverified")
    if model["id"] in policy["denied_models"] or model["resolved_model"] in policy["denied_models"]:
        reasons.append("Denied by model policy")
    if model["identity"] == "alias" and model["resolved_model"] is None:
        reasons.append("Alias resolution is unverified for this provider/client")
    if (
        catalog["harness"] == "claude-code-native"
        and requirement["operation"] == "subagent"
        and (
            model["identity"] != "exact"
            or re.fullmatch(
                r"(opus|sonnet|haiku|fable|best|opusplan|default|inherit)(\[.*\])?",
                model["id"],
                re.IGNORECASE,
            )
        )
    ):
        reasons.append(
            "Claude subagent selection requires an evidenced full native model ID; family aliases depend on parent and selection mechanism"
        )
    for providers in (policy["allowed_providers"], requirement["allowed_providers"]):
        if providers and model["provider"] not in providers:
            reasons.append("Provider excluded by constraints")
    if any(c not in model["capabilities"] for c in requirement["capabilities"]):
        reasons.append("Required capability is unverified or unsupported")
    if requirement["min_context_tokens"] is not None and (
        model["context_tokens"] is None or model["context_tokens"] < requirement["min_context_tokens"]
    ):
        reasons.append("Required context capacity is unverified or insufficient")
    if requirement["effort"] is not None and requirement["effort"] not in model["efforts"]:
        reasons.append("Requested effort is unsupported")
    if not (policy["allow_preview"] and requirement["allow_preview"]) and model["lifecycle"] != "stable":
        reasons.append("Stable model required; lifecycle is preview or unknown")
    if requirement["exact_model"] is not None and (
        model["identity"] != "exact" or model["id"] != requirement["exact_model"]
    ):
        reasons.append("Exact-model requirement is unsatisfied")
    if requirement["max_cost_tier"] is not None and (
        model["cost_tier"] is None or model["cost_tier"] > requirement["max_cost_tier"]
    ):
        reasons.append("Cost-tier bound is unverified or exceeded")
    if catalog["harness"] == "copilot-local" and requirement["operation"] == "subagent":
        if model["cost_tier"] is None or requirement["parent_cost_tier"] is None:
            reasons.append(
                "Copilot Local parent/child cost tier must be established before explicit delegation"
            )
        elif model["cost_tier"] > requirement["parent_cost_tier"]:
            reasons.append("Copilot Local child exceeds parent cost tier")
    # Auto's lifecycle is the catalog's own claim for the strategy: a stable auto
    # entry passes the stable-lifecycle rule above like any other model.
    if model["identity"] == "auto" and (
        requirement["allowed_providers"]
        or policy["allowed_providers"]
        or policy["denied_models"]
        or requirement["min_context_tokens"] is not None
        or requirement["effort"] is not None
        or requirement["max_cost_tier"] is not None
    ):
        reasons.append("Auto cannot prove these per-model hard constraints")
    return list(dict.fromkeys(reasons))


def freshness(catalog, request, policy, at=None):
    age = _timestamp(at or now()) - _timestamp(catalog["observed_at"])
    reasons = []
    if age < -300:
        reasons.append("Catalog observation is in the future")
    if age > policy["catalog_max_age_hours"] * 3600:
        reasons.append("Catalog is stale; refresh availability")
    if catalog["session_id"] != request["session_id"]:
        reasons.append("Catalog belongs to a different session; revalidate availability")
    return reasons


def create_model_plan(root, request, catalog, at=None, *, selector=None):
    at = at or now()
    if _timestamp(at) > time.time() + 300:
        raise FactoryError("Model plan creation time is in the future")
    request, catalog = copy.deepcopy(request), copy.deepcopy(catalog)
    validate_document(root, "request", request)
    validate_document(root, "catalog", catalog)
    if request["profile"] != catalog["profile"] or request["harness"] != catalog["harness"]:
        raise FactoryError("Request and inventory refer to different clients/harnesses")
    controls = model_controls(root)
    policy, recommendations = controls["policy"], controls["recommendations"]
    entries = guidance_entries(request, recommendations)
    for entry in entries:
        if _timestamp(entry["checked_at"]) > _timestamp(at) + 300:
            raise FactoryError(f"Guidance {entry['key']} is dated in the future")
    for item in request["assignments"]:
        if item["intent"] != policy["role_intents"][item["role"]]:
            raise FactoryError(
                f"Assignment {item['id']} intent {item['intent']} differs from the policy intent "
                f"{policy['role_intents'][item['role']]} for role {item['role']}; use the policy intent"
            )
    stale = freshness(catalog, request, policy, at)
    if stale:
        raise FactoryError(
            "Cannot plan with this catalog: " + "; ".join(stale) + ". Rediscover the catalog for this session"
        )
    warnings = []
    if catalog["provenance"]["kind"] != "runtime":
        warnings.append(f"Inventory provenance: {catalog['provenance']['kind']}; not runtime-attested")
    selected_parent = None

    def select(item):
        requirements = copy.deepcopy(item["requirements"])
        parent = (selected_parent or {}).get("model") or next(
            (m for m in catalog["models"] if m["id"] == catalog["parent_model"]), None
        )
        parent_errors = []
        if catalog["harness"] == "copilot-local" and requirements["operation"] == "subagent":
            if any(a["requirements"]["operation"] == "main" for a in request["assignments"]) and not (
                selected_parent or {}
            ).get("model"):
                parent_errors.append("Main-session assignment is unresolved")
            parent_tier = parent["cost_tier"] if parent else None
            if (
                requirements["parent_cost_tier"] is not None
                and requirements["parent_cost_tier"] != parent_tier
            ):
                parent_errors.append("Claimed parent tier differs from selected or observed parent")
            requirements["parent_cost_tier"] = parent_tier
        rejected, eligible = [], []
        for model in catalog["models"]:
            reasons = parent_errors + eligibility(model, requirements, catalog, policy)
            guidance = _guidance_for(model, catalog, entries)
            for prerequisite in _identity_guidance(
                model, catalog, recommendations["entries"] + request["research"]
            ):
                reasons.extend(_version_reasons(prerequisite, catalog))
            preferred = item["preferred_model"] == model["id"]
            if not guidance and not preferred:
                reasons.append(
                    "No reviewed task-fit guidance; supply a reasoned explicit preference after research"
                )
            if (
                selector is None
                and guidance
                and guidance["key"] not in policy["preferences"][item["intent"]]
                and not preferred
            ):
                reasons.append("Outside configured task-fit preferences")
            if (
                guidance
                and _timestamp(at) - _timestamp(guidance["checked_at"])
                > policy["guidance_max_age_days"] * 86400
            ):
                warnings.append(
                    f"Guidance {guidance['key']} is stale; research official sources and record current rationale"
                )
            if reasons:
                rejected.append({"model": model["id"], "reasons": list(dict.fromkeys(reasons))})
            else:
                eligible.append({"model": model, "guidance": guidance, "preferred": preferred})
        prefs = policy["preferences"][item["intent"]]
        eligible.sort(
            key=lambda e: (
                not e["preferred"],
                prefs.index(e["guidance"]["key"]) if e["guidance"] and e["guidance"]["key"] in prefs else -1,
                e["model"]["id"],
            )
        )
        if selector is None:
            selected = (
                next((e for e in eligible if e["preferred"]), None)
                if item["preferred_model"]
                else next(iter(eligible), None)
            )
        else:
            eligible.sort(key=lambda entry: entry["model"]["id"])
            chosen = selector(copy.deepcopy(item), copy.deepcopy(requirements), copy.deepcopy(eligible))
            selected = next((entry for entry in eligible if entry["model"]["id"] == chosen), None)
            if chosen is not None and (
                selected is None or (item["preferred_model"] and chosen != item["preferred_model"])
            ):
                raise FactoryError("Model selector violated eligible candidates or explicit preference")
        return {
            "id": item["id"],
            "role": item["role"],
            "intent": item["intent"],
            "requirements": requirements,
            "status": "selected" if selected else "unresolved",
            "model": selected["model"] if selected else None,
            "effort": (
                requirements["effort"]
                if requirements["effort"] is not None
                else selected["model"]["default_effort"]
            )
            if selected
            else None,
            "rationale": (
                item["rationale"]
                if selected["preferred"]
                else (
                    f"{selected['guidance']['strengths']} The only reviewed candidate satisfying the hard constraints; no JEV request was needed. This is not a comparative benchmark."
                    if selector is not None and len(eligible) == 1
                    else f"{selected['guidance']['strengths']} Selected by JEV among reviewed candidates satisfying the hard constraints; this is a local-unattested selection, not a comparative benchmark."
                    if selector is not None
                    else f"{selected['guidance']['strengths']} Selected by the configured {item['intent']} preference order among eligible choices; this is not a comparative benchmark."
                )
            )
            if selected
            else "No eligible selection. Inspect rejected candidates, refresh missing metadata or revise the plan within existing constraints.",
            "sources": selected["guidance"]["sources"] if selected and selected["guidance"] else [],
            "fallbacks": [e["model"]["id"] for e in eligible if e is not selected],
            "rejected": rejected,
        }

    main = next(
        (a for a in request["assignments"] if a["requirements"]["operation"] == "main"),
        None,
    )
    if main:
        selected_parent = select(main)
    assignments = [selected_parent if a is main else select(a) for a in request["assignments"]]
    plan = {
        "schema_version": 1,
        "kind": "plan",
        "id": request["id"],
        "created_at": at,
        "request": request,
        "catalog": catalog,
        "policy_hash": controls["policy_hash"],
        "recommendations_hash": controls["recommendations_hash"],
        "assignments": assignments,
        "warnings": list(dict.fromkeys(warnings)),
    }
    if selector is None:
        from .routing import attach_factory_routing

        return validate_document(root, "plan", attach_factory_routing(root, plan))
    # The explicit router/replayer attaches required receipts after all sequential
    # choices have completed. This intermediate value is never published.
    return plan


def _reject_constant(value):
    raise ValueError(f"non-finite JSON number {value}")


def validate_plan(root, plan, current=True):
    validate_document(root, "plan", plan)
    if _timestamp(plan["created_at"]) > time.time() + 300:
        raise FactoryError("Model plan creation time is in the future")
    validate_document(root, "request", plan["request"])
    validate_document(root, "catalog", plan["catalog"])
    if plan["id"] != plan["request"]["id"]:
        raise FactoryError("Model plan ID mismatch")
    _unique([a["id"] for a in plan["assignments"]], "plan assignment ID")
    from .routing import replay_model_plan, validate_recorded_routing

    validate_recorded_routing(plan)
    if current:
        expected = replay_model_plan(root, plan)
        if canonical(expected) != canonical(plan):
            raise FactoryError(
                "Model plan differs from validated policy, guidance or eligibility; regenerate it"
            )
    return plan


def resolve_assignment(root, mission, binding, current=False):
    validate_document(root, "binding", binding)
    if not binding["plan_path"].startswith(f".factory/missions/{mission['id']}/models/"):
        raise FactoryError("Model plan belongs outside this mission")
    try:
        data = safe_path(root, binding["plan_path"]).read_bytes()
    except OSError as exc:
        raise FactoryError(f"Cannot read model plan {binding['plan_path']}: {exc.strerror or exc}") from None
    if sha256(data) != binding["plan_hash"]:
        raise FactoryError("Model plan content hash changed")
    try:
        value = json.loads(data, parse_constant=_reject_constant)
    except ValueError as exc:
        raise FactoryError(f"Cannot read model plan {binding['plan_path']}: {exc}") from None
    plan = validate_plan(root, value, current=current)
    assignment = next((a for a in plan["assignments"] if a["id"] == binding["assignment_id"]), None)
    if not assignment or model_hash(assignment) != binding["assignment_hash"]:
        raise FactoryError("Model assignment content hash mismatch")
    return {"plan": plan, "assignment": assignment}


def dispatch_assignment(root, plan, assignment_id, catalog):
    validate_plan(root, plan)
    validate_document(root, "catalog", catalog)
    assignment = next((a for a in plan["assignments"] if a["id"] == assignment_id), None)
    if not assignment:
        raise FactoryError(
            f"Assignment {assignment_id!r} is missing from the plan; use one of: "
            + ", ".join(a["id"] for a in plan["assignments"])
        )
    if assignment["status"] != "selected":
        raise FactoryError(f"Assignment {assignment_id} is unresolved; revise the request and plan again")
    if catalog["profile"] != plan["request"]["profile"] or catalog["harness"] != plan["request"]["harness"]:
        raise FactoryError("Dispatch client/harness differs from plan")
    if catalog["billing_context"] != plan["catalog"]["billing_context"]:
        raise FactoryError("Dispatch billing context differs from plan; regenerate it")
    controls = model_controls(root)
    policy, recommendations = controls["policy"], controls["recommendations"]
    # The dispatch catalog must be current and belong to the plan's session: a new
    # session revalidates availability by planning again.
    reasons = freshness(catalog, plan["request"], policy)
    actual = next((m for m in catalog["models"] if m["id"] == assignment["model"]["id"]), None)
    if actual is None:
        reasons.append("Selected model is no longer advertised")
    else:
        reasons.extend(eligibility(actual, assignment["requirements"], catalog, policy))
        if canonical(actual) != canonical(assignment["model"]):
            reasons.append("Model metadata changed; regenerate the assignment")
    if catalog["harness"] == "copilot-local" and assignment["requirements"]["operation"] == "subagent":
        parent = next((m for m in catalog["models"] if m["id"] == catalog["parent_model"]), None)
        if (
            parent is None
            or parent["cost_tier"] is None
            or parent["cost_tier"] != assignment["requirements"]["parent_cost_tier"]
        ):
            reasons.append(
                "Observed parent tier differs from planned delegation; select/revalidate the main model first"
            )
    guidance = _guidance_for(assignment["model"], catalog, guidance_entries(plan["request"], recommendations))
    for prerequisite in _identity_guidance(
        assignment["model"],
        catalog,
        recommendations["entries"] + plan["request"]["research"],
    ):
        reasons.extend(_version_reasons(prerequisite, catalog))
    if reasons:
        raise FactoryError("Cannot dispatch model assignment: " + "; ".join(dict.fromkeys(reasons)))
    warnings = list(plan["warnings"])
    if catalog["harness"] == "claude-code-native":
        warnings.append(
            "Claude environment or managed force settings may override native selection; requested settings are not runtime execution evidence"
        )
    if (
        guidance
        and time.time() - _timestamp(guidance["checked_at"]) > policy["guidance_max_age_days"] * 86400
    ):
        warnings.append(
            f"Guidance {guidance['key']} is stale at dispatch; refresh official research before relying on the recommendation"
        )
    main = assignment["requirements"]["operation"] == "main"
    settings = {"model": assignment["model"]["id"]}
    if assignment["effort"] and catalog["profile"] != "copilot":
        settings["model_reasoning_effort" if catalog["profile"] == "codex" else "effort"] = assignment[
            "effort"
        ]
    return {
        "assignment_hash": model_hash(assignment),
        "profile": catalog["profile"],
        "harness": catalog["harness"],
        "operation": assignment["requirements"]["operation"],
        "requested_model": assignment["model"]["id"],
        "requested_effort": assignment["effort"],
        "mechanism": "host-model-picker" if main else "native-subagent-control",
        "settings": settings,
        "instructions": "Select through the current client model picker for the session. This report does not switch the current main model."
        if main
        else "Use the actual exposed subagent tool parameters when supported. These are native agent configuration fields, not a universal tool-call signature. Keep parent permissions and factory role instructions; never edit generated files directly.",
        "observed_model": None,
        "applied": False,
        "warnings": list(dict.fromkeys(warnings)),
    }


def validate_task_observation(root, mission, task, observation):
    if not task.get("model_assignment"):
        if observation is not None:
            raise FactoryError("Unbound task cannot claim a model assignment")
        return
    if observation is None:
        raise FactoryError(
            f"Task {task['id']} is bound to a model assignment; its result needs a model_observation for "
            "attempt " + str(task["attempts"]) + ' (use provenance "unknown" when identity was not observed)'
        )
    validate_document(root, "observation", observation)
    resolved = resolve_assignment(root, mission, task["model_assignment"])
    assignment, plan = resolved["assignment"], resolved["plan"]
    attempt = next(
        (a for a in task.get("model_attempts", []) if a["attempt"] == task["attempts"]),
        None,
    )
    if (
        not attempt
        or attempt["assignment_hash"] != task["model_assignment"]["assignment_hash"]
        or observation["attempt"] != task["attempts"]
        or observation["assignment_hash"] != attempt["assignment_hash"]
    ):
        raise FactoryError("Model observation does not match the current task attempt")
    if (
        observation["requested_model"] != (assignment["model"] or {}).get("id")
        or observation["requested_effort"] != assignment["effort"]
    ):
        raise FactoryError("Requested model/effort differs from task assignment")
    requirement, policy = assignment["requirements"], model_controls(root)["policy"]
    if not assignment["model"] or eligibility(assignment["model"], requirement, plan["catalog"], policy):
        raise FactoryError("Assigned model violates current policy or model constraints")
    if observation["observed_model"] in policy["denied_models"]:
        raise FactoryError("Observed model is denied by current policy")
    if requirement["exact_model"] is not None and (
        observation["provenance"] != "runtime" or observation["observed_model"] != requirement["exact_model"]
    ):
        raise FactoryError("Exact-model requirement needs matching runtime evidence")
    if requirement["effort"] is not None and (
        observation["provenance"] != "runtime" or observation["observed_effort"] != requirement["effort"]
    ):
        raise FactoryError("Explicit effort requirement needs matching runtime evidence")
    resolved_model = assignment["model"]["resolved_model"] or observation["requested_model"]
    substitution = (
        observation["observed_model"] is not None and observation["observed_model"] != resolved_model
    )
    if substitution and assignment["model"]["identity"] != "auto":
        if not (observation["fallback_reason"] or "").strip():
            raise FactoryError("Observed model substitution needs a recorded reason")
        fallback = next(
            (
                m
                for m in plan["catalog"]["models"]
                if (m["resolved_model"] or m["id"]) == observation["observed_model"]
            ),
            None,
        )
        if not fallback or fallback["id"] not in assignment["fallbacks"]:
            raise FactoryError("Observed substitute is not an eligible recorded fallback; reconcile the plan")
        if eligibility(fallback, requirement, plan["catalog"], policy):
            raise FactoryError("Observed fallback violates model constraints")


# Environment variables never passed to the Codex metadata child: factory secrets
# the client has no use for.
DISCOVERY_SCRUBBED_ENV = ("TYPESAFE_API_KEY",)
STDERR_TAIL_BYTES = 1024
CODEX_MODALITIES = ("text", "image")
CODEX_ENRICHMENT_WARNING = (
    "Codex model/list reports no tool support, lifecycle or subagent availability, so planning rejects "
    "these models until the catalog is enriched: add tools to capabilities, set lifecycle and operations "
    "from official research, and record the source in provenance.reference"
)


def discover_codex(
    root,
    binary,
    session_id,
    provider="unknown",
    billing_context="unknown",
    timeout_ms=15000,
):
    """Read only initialize/model-list metadata; bounded protocol, time and memory.

    Returns {"catalog": ..., "warnings": [...]}.
    """
    if not binary or not Path(binary).is_absolute():
        raise FactoryError("Codex discovery requires the actual absolute client binary")
    if type(timeout_ms) is not int or not 50 <= timeout_ms <= 60000:
        raise FactoryError("Invalid discovery timeout")
    env = {key: value for key, value in os.environ.items() if key not in DISCOVERY_SCRUBBED_ENV}
    try:
        child = subprocess.Popen(
            [str(binary), "app-server"],
            cwd=root,
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except OSError as exc:
        raise FactoryError("Codex metadata process could not start") from exc
    selector = selectors.DefaultSelector()
    selector.register(child.stdout, selectors.EVENT_READ)
    selector.register(child.stderr, selectors.EVENT_READ)
    os.set_blocking(child.stderr.fileno(), False)
    buffer, total, seq = b"", 0, 0
    stderr_tail = bytearray()
    deadline = time.monotonic() + timeout_ms * 2 / 1000

    def read_stderr():
        # Keep only a bounded tail; a chatty client cannot fill the pipe and stall.
        for _ in range(64):
            try:
                data = os.read(child.stderr.fileno(), 65536)
            except (BlockingIOError, OSError):
                return
            if not data:
                # Closed stderr stays readable; stop selecting it to avoid a busy loop.
                try:
                    selector.unregister(child.stderr)
                except (KeyError, ValueError):
                    pass
                return
            stderr_tail.extend(data)
            del stderr_tail[:-STDERR_TAIL_BYTES]

    def write(value, end=None):
        # A client can stop reading while supplying a large opaque pagination
        # cursor. Never let pipe backpressure escape the discovery deadline.
        end = min(deadline, end or (time.monotonic() + timeout_ms / 1000))
        payload = memoryview(json.dumps(value).encode() + b"\n")
        try:
            os.set_blocking(child.stdin.fileno(), False)
            with selectors.DefaultSelector() as writer:
                writer.register(child.stdin, selectors.EVENT_WRITE)
                while payload:
                    remaining = end - time.monotonic()
                    if remaining <= 0 or not writer.select(remaining):
                        raise FactoryError("Codex metadata write timeout or deadline")
                    try:
                        written = os.write(child.stdin.fileno(), payload[:65536])
                    except BlockingIOError:
                        continue
                    if written == 0:
                        raise FactoryError("Codex metadata transport closed")
                    payload = payload[written:]
        except OSError as exc:
            raise FactoryError("Codex metadata transport closed") from exc

    def request(method, params):
        nonlocal buffer, total, seq
        if method not in ("initialize", "model/list"):
            raise FactoryError("Unsupported discovery method")
        seq += 1
        end = min(deadline, time.monotonic() + timeout_ms / 1000)
        write({"id": seq, "method": method, "params": params}, end)
        while True:
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                try:
                    value = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(value, dict) or value.get("id") != seq:
                    continue
                if value.get("error"):
                    raise FactoryError("Codex metadata request failed; inspect the client connection locally")
                return value.get("result")
            remaining = end - time.monotonic()
            events = selector.select(remaining) if remaining > 0 else []
            if not events:
                raise FactoryError(f"Codex metadata timeout or deadline: {method}")
            if any(key.fileobj is child.stderr for key, _ in events):
                read_stderr()
            if not any(key.fileobj is child.stdout for key, _ in events):
                continue
            data = os.read(child.stdout.fileno(), 65536)
            if not data:
                raise FactoryError("Codex metadata process exited before completing discovery")
            total += len(data)
            if total > 4 * 1024 * 1024:
                raise FactoryError("Codex metadata response exceeded size limit")
            buffer += data

    warnings, dropped = [], set()
    try:
        initialized = request(
            "initialize",
            {
                "clientInfo": {"name": "factory_models", "version": "1.0.0"},
                "capabilities": {"experimentalApi": True},
            },
        )
        write({"method": "initialized"})
        models, cursors, cursor = [], set(), None
        for page in range(20):
            response = request(
                "model/list",
                {
                    "limit": 100,
                    "includeHidden": False,
                    **({"cursor": cursor} if cursor else {}),
                },
            )
            if not isinstance(response, dict) or not isinstance(response.get("data"), list):
                raise FactoryError("Invalid Codex catalog response")
            for item in response["data"]:
                if not isinstance(item, dict):
                    raise FactoryError("Invalid Codex catalog model")
                if item.get("hidden"):
                    continue
                if not isinstance(item.get("model"), str):
                    raise FactoryError("Invalid Codex catalog model: model/list item has no string model ID")
                efforts = item.get("supportedReasoningEfforts", [])
                if not isinstance(efforts, list) or any(
                    not isinstance(e, dict) or "reasoningEffort" not in e for e in efforts
                ):
                    raise FactoryError("Invalid Codex effort metadata")
                modalities = item.get("inputModalities", [])
                if not isinstance(modalities, list):
                    raise FactoryError(f"Invalid Codex input modalities for {item['model']}")
                for modality in modalities:
                    if modality not in CODEX_MODALITIES:
                        dropped.add(str(modality)[:40])
                models.append(
                    {
                        "id": item["model"],
                        "provider": provider,
                        "identity": "exact",
                        "resolved_model": None,
                        "availability": "visible",
                        "operations": ["main"],
                        "capabilities": [m for m in dict.fromkeys(modalities) if m in CODEX_MODALITIES],
                        "context_tokens": None,
                        "efforts": [e["reasoningEffort"] for e in efforts],
                        "default_effort": item.get("defaultReasoningEffort"),
                        "lifecycle": "unknown",
                        "cost_tier": None,
                        "guidance_key": None,
                    }
                )
            cursor = response.get("nextCursor")
            # Codex ends pagination with null; an empty cursor also means no next page.
            if cursor is None or cursor == "":
                break
            if not isinstance(cursor, str) or cursor in cursors or page == 19:
                raise FactoryError("Codex catalog pagination did not complete")
            cursors.add(cursor)
        catalog = catalog_template(profile="codex", harness="codex-native", session_id=session_id)
        catalog.update(
            client_version=initialized.get("userAgent", "unknown")
            if isinstance(initialized, dict) and isinstance(initialized.get("userAgent"), str)
            else "unknown",
            billing_context=billing_context,
            provenance={
                "kind": "runtime",
                "reference": "Installed client app-server model/list; visibility is not inference or subagent eligibility proof. Missing lifecycle/tool/delegation metadata must be researched separately.",
            },
            models=models,
        )
        if dropped:
            warnings.append(
                "Ignored input modalities the catalog does not model: " + ", ".join(sorted(dropped))
            )
        if models:
            warnings.append(CODEX_ENRICHMENT_WARNING)
        return {"catalog": validate_document(root, "catalog", catalog), "warnings": warnings}
    except FactoryError as exc:
        read_stderr()
        if not stderr_tail:
            raise
        from .redaction import redact

        tail = redact(stderr_tail.decode("utf-8", "replace").strip())[0][-STDERR_TAIL_BYTES:]
        raise FactoryError(f"{exc}; client stderr tail: {tail}", exc.exit_code) from None
    finally:
        selector.close()
        child.stdin.close()
        child.stdout.close()
        child.stderr.close()
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=2)


BILLING_CONTEXTS = ("subscription", "api", "copilot", "byok", "unknown")
# Claude Code picker entries that name a selection strategy rather than one model.
CLAUDE_STRATEGY_ALIASES = ("best", "default", "opusplan", "inherit")
PICKER_REFERENCE_LIMIT = 300
GUIDANCE_REFERENCE = (
    "Reviewed factory guidance; availability not verified for this account — confirm with the /model "
    "picker and rerun discover with --picker. Capabilities, operations and lifecycle come from factory "
    "defaults for reviewed guidance; context, efforts and cost tier are unknown."
)
EMPTY_CATALOG_MESSAGE = (
    "The model catalog has no models; run `software-factory models discover --profile claude --picker "
    '"<your /model list>"` (or fill the catalog) — JEV and factory-models can only choose among listed '
    "models."
)


def _claude_guidance(entries):
    return [e for e in entries if e["provider"] == "anthropic" and "claude" in e["profiles"]]


def _guidance_model(entry, availability):
    """Catalog entry derived from reviewed guidance; unestablished metadata stays unknown.

    Guidance lists the exact native ID, provider and key. Everything else is a
    conservative factory default: Claude Code runs every model with text and
    tool use, accepts full model IDs for the main session and subagents, and
    guidance covers generally available models only. Image input, context
    capacity, efforts and cost tier are not recorded in guidance, so they are
    omitted or null and requirements that need them stay unresolved.
    """
    return {
        "id": entry["model_ids"][0],
        "provider": entry["provider"],
        "identity": "exact",
        "resolved_model": None,
        "availability": availability,
        "operations": ["main", "subagent"],
        "capabilities": ["text", "tools"],
        "context_tokens": None,
        "efforts": [],
        "default_effort": None,
        "lifecycle": "stable",
        "cost_tier": None,
        "guidance_key": entry["key"],
    }


def _picker_entry(token, entries):
    """Guidance for one picker name: a native ID, display name, family alias or family and version."""
    lowered = " ".join(token.lower().split())
    exact = next(
        (e for e in entries if lowered in (i.lower() for i in e["model_ids"] + e.get("display_names", []))),
        None,
    )
    match = re.fullmatch(r"(?:claude[\s-]+)?([a-z]+)(?:[\s-]+v?(\d+(?:[.-]\d+)*))?", lowered)
    if exact or not match:
        return exact
    family, version = match.groups()
    if version:
        # "Opus 5.5" or "claude-opus-5-5" names exactly that native ID.
        target = f"claude-{family}-{version.replace('.', '-')}"
        return next((e for e in entries if target in (i.lower() for i in e["model_ids"])), None)
    pattern = re.compile(rf"claude-{re.escape(family)}(-.+)?")
    return next((e for e in entries if any(pattern.fullmatch(i.lower()) for i in e["model_ids"])), None)


def parse_claude_picker(text, entries):
    """Map a Claude Code /model "Available:" list to reviewed guidance entries.

    Accepts comma, semicolon, newline or space separated names, an optional
    "Available:" or "Available models:" prefix, list markers, parenthetical
    notes such as "(default)" or "(claude-opus-5-5)" and "[1m]" suffixes.
    Returns (matched entries in picker order, skipped entries with reasons, notes).
    """
    body = re.sub(r"^\s*available(?:\s+models)?\s*:\s*", "", text.strip(), flags=re.IGNORECASE)
    matched, skipped, notes = [], [], []

    def consider(token, split=True):
        found = re.fullmatch(r"(.*?)\s*(\[[^\]]*\])?", token)
        base, suffix = found.group(1), found.group(2)
        inner = [part.strip() for part in re.findall(r"\(([^()]*)\)", base) if part.strip()]
        # Parentheses, including empty ones, never form part of a model name.
        base = " ".join(re.sub(r"\([^()]*\)", " ", base).split())
        label = " ".join(f"{base}{suffix or ''}".split()) or token
        if not base and not inner:
            return
        if base.lower() in CLAUDE_STRATEGY_ALIASES:
            skipped.append(
                {"entry": label, "reason": "Selection strategy or default alias, not a single model"}
            )
            return
        entry = _picker_entry(base, entries) if base else None
        entry = entry or next(filter(None, (_picker_entry(part, entries) for part in inner)), None)
        # "Sonnet 4.5" names one version; never split it into a family alias.
        versioned = re.fullmatch(r"(?:claude[\s-]+)?[a-z]+[\s-]+v?\d+(?:[.-]\d+)*", base.lower())
        if entry is None and " " in base and not versioned:
            words = base.split()
            if split and any(
                w.lower() in CLAUDE_STRATEGY_ALIASES or _picker_entry(re.sub(r"\[.*", "", w), entries)
                for w in words
            ):
                # A space-separated list such as "sonnet opus haiku".
                for word in words:
                    consider(word, split=False)
            # Otherwise prose such as "a full model ID" is picker help text, not a model.
            return
        if entry is None:
            skipped.append(
                {
                    "entry": label,
                    "reason": "No reviewed factory guidance for this alias or model ID; research it and "
                    "fill the catalog manually",
                }
            )
            return
        if suffix:
            notes.append(
                f"{label} maps to {entry['model_ids'][0]}; its extended context is not recorded because "
                "reviewed guidance does not state context capacity"
            )
        if entry not in matched:
            matched.append(entry)

    for raw in re.split(r"[,;\n]", body):
        token = re.sub(r"^(?:[-*\u2022>\u276f]\s*|\d+[.)]\s+)+", "", raw.strip())
        token = re.sub(r"^or\s+", "", token.rstrip(".").strip(), flags=re.IGNORECASE).strip()
        if token:
            consider(token)
    return matched, skipped, notes


def discover_claude(root, session_id, picker=None, client_version="unknown", billing_context="unknown"):
    """Catalog for Claude Code, which exposes no machine-readable model list.

    Without picker text the catalog lists reviewed guidance with unknown availability
    (planning rejects those models). With the operator's /model list, only matched
    models are listed as visible under user_report provenance.
    """
    entries = _claude_guidance(model_controls(root)["recommendations"]["entries"])
    catalog = catalog_template(profile="claude", harness="claude-code-native", session_id=session_id)
    catalog.update(client_version=client_version or "unknown", billing_context=billing_context)
    skipped, notes, warnings = [], [], []
    if picker is None:
        catalog.update(
            provenance={"kind": "fixture", "reference": GUIDANCE_REFERENCE},
            models=[_guidance_model(entry, "unknown") for entry in entries],
        )
        warnings.append(
            "Model availability is unknown for this account, so planning rejects these models. Copy the "
            'Claude Code /model "Available:" list and rerun discover with --picker "<list>".'
        )
    else:
        if not picker.strip():
            raise FactoryError('--picker needs the Claude Code /model "Available:" list text')
        matched, skipped, notes = parse_claude_picker(picker, entries)
        if not matched:
            listed = ", ".join(s["entry"] for s in skipped)
            raise FactoryError(
                "No --picker entry matched reviewed factory guidance"
                + (f" ({listed})" if listed else "")
                + '; paste the /model list, e.g. "sonnet, opus, haiku, fable", or fill the catalog manually'
            )
        quoted = " ".join(picker.split())
        if len(quoted) > PICKER_REFERENCE_LIMIT:
            quoted = quoted[: PICKER_REFERENCE_LIMIT - 3] + "..."
        catalog.update(
            provenance={
                "kind": "user_report",
                "reference": f'Claude Code /model picker reported by the operator: "{quoted}". Aliases are '
                "mapped to reviewed full model IDs; this account's alias resolution is not runtime-verified "
                "(check /status).",
            },
            models=[_guidance_model(entry, "visible") for entry in matched],
        )
        for entry in matched:
            for reason in _version_reasons(entry, catalog):
                warnings.append(
                    f"{entry['model_ids'][0]}: {reason}; pass --client-version (see claude --version)"
                )
    return {
        "catalog": validate_document(root, "catalog", catalog),
        "skipped": skipped,
        "notes": notes,
        "warnings": warnings,
    }


def plan_warnings(plan):
    """Explain a plan in which no assignment had any eligible candidate."""
    assignments = plan["assignments"]
    if not assignments or any(a["status"] != "unresolved" or a["fallbacks"] for a in assignments):
        return []
    counts = {}
    for assignment in assignments:
        for rejection in assignment["rejected"]:
            for reason in rejection["reasons"]:
                counts[reason] = counts.get(reason, 0) + 1
    common = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:3]
    return [
        "Every assignment is unresolved with no eligible candidates, so no selector (JEV or factory-models) "
        "ran. Most common rejection reasons: "
        + ("; ".join(f"{reason} ({count})" for reason, count in common) or "none recorded")
        + ". Fix the catalog or requirements, then plan again."
    ]


STDIN_INPUTS = ("input", "catalog", "plan")


def _stdin_json(root, name):
    from .workflow import read_text_input

    text = read_text_input(root, "-", f"--{name} JSON")[1]
    try:
        return json.loads(text, parse_constant=_reject_constant)
    except ValueError as exc:
        raise FactoryError(f"Cannot read --{name} JSON from stdin: {exc}") from None


def _provenance(kind, value):
    """The trust of one validated document, stated per kind."""
    if kind == "catalog":
        return value["provenance"]["kind"]
    if kind == "observation":
        return value["provenance"]
    return "local-unattested"


def handler(args):
    from .calibration import (
        assert_private_directory,
        model_calibration,
        outcome_template,
        record_model_outcome,
    )

    root, command = args.root, args.models_command or "sources"

    def validate_output():
        if not args.output:
            return
        if command == "outcome-record":
            raise FactoryError(
                "outcome-record publishes an immutable local record; --output is not supported"
            )
        if not re.fullmatch(r"\.factory/local/models/[A-Za-z0-9_-]+\.json", args.output):
            raise FactoryError(
                "--output must be a JSON file directly under .factory/local/models/; "
                "use .factory/local/models/<name>.json"
            )
        target = safe_path(root, args.output)
        if target.exists() and not target.is_file():
            raise FactoryError("--output must name a regular JSON file")
        for parent in target.parents:
            if parent.exists() and not parent.is_dir():
                raise FactoryError("--output parent paths must be directories")
        assert_private_directory(root, ".factory/local/models", create=False)

    validate_output()
    stdin = [name for name in STDIN_INPUTS if getattr(args, name, None) == "-"]
    if len(stdin) > 1:
        raise FactoryError(
            "Only one input can read stdin; " + " and ".join(f"--{n}" for n in stdin) + " both use -"
        )
    if args.billing is not None and args.billing not in BILLING_CONTEXTS:
        raise FactoryError(f"Invalid --billing {args.billing!r}; use one of: {', '.join(BILLING_CONTEXTS)}")
    if type(args.timeout_ms) is not int or not 50 <= args.timeout_ms <= 60000:
        raise FactoryError(f"Invalid --timeout-ms {args.timeout_ms!r}; use 50-60000")
    controls = model_controls(root)
    # sources and template need no project; other commands validate their own inputs.
    available_profiles = profiles(load_config(root)) if safe_path(root, "factory.json").is_file() else []
    if args.profile is not None and args.profile not in MODEL_HARNESSES:
        raise FactoryError(f"Invalid --profile {args.profile!r}; use one of: {', '.join(MODEL_HARNESSES)}")
    profile = args.profile or (available_profiles[0] if len(available_profiles) == 1 else None)
    harness = args.harness or {
        "codex": "codex-native",
        "claude": "claude-code-native",
    }.get(profile)
    if profile and args.harness and args.harness not in MODEL_HARNESSES[profile]:
        raise FactoryError(
            f"Invalid --harness {args.harness!r} for profile {profile}; use "
            + " or ".join(MODEL_HARNESSES[profile])
        )

    def require(extra=()):
        hint = (
            "one of this project's profiles: " + ", ".join(available_profiles)
            if len(available_profiles) > 1
            else "claude, codex or copilot"
        )
        flags = (
            ("--profile", profile, hint),
            # The harness is derivable once the profile is known, so only then is it asked for.
            *((("--harness", harness, "copilot-local or copilot-agent-host"),) if profile else ()),
            ("--session", args.session, "a non-sensitive label such as WORK-20260926-A"),
            *extra,
        )
        missing = [f"{flag} ({hint})" for flag, value, hint in flags if not value]
        if missing:
            raise FactoryError(f"models {command} is missing required " + ", ".join(missing))

    picker = getattr(args, "picker", None)
    client_version = getattr(args, "client_version", None)
    if picker is not None or client_version is not None:
        if command == "discover" and profile is None:
            require()
        if not (command == "discover" and profile == "claude"):
            raise FactoryError("--picker and --client-version apply only to models discover --profile claude")

    def context(extra=()):
        require(extra)
        return {"profile": profile, "harness": harness, "session_id": args.session}

    def read_input(name):
        path = getattr(args, name)
        if not path:
            raise FactoryError(f"--{name} requires a repository path, or - for stdin")
        if path == "-":
            return _stdin_json(root, name)
        return read_json(root, path)

    extra = {}
    if command == "sources":
        result = {
            **controls,
            "entries": [
                e for e in controls["recommendations"]["entries"] if not profile or profile in e["profiles"]
            ],
            "instructions": "Read the current official pages for task-specific research. Vendor recommendations are not account access or comparative benchmarks. Store refreshed research locally; canonical changes require maintenance.",
        }
    elif command == "template":
        if args.kind == "catalog":
            result = catalog_template(**context())
        elif args.kind == "request":
            ctx = context(
                (
                    ("--id", args.id, "a plan ID such as PLAN-001"),
                    ("--objective", args.objective, "the task objective text"),
                )
            )
            result = request_template(**ctx, id=args.id, objective=args.objective, policy=controls["policy"])
        else:
            raise FactoryError("models template requires --kind catalog or --kind request")
        validate_document(root, args.kind, result)
    elif command == "discover":
        ctx = context()
        if profile in ("codex", "claude"):
            if profile == "codex":
                discovered = discover_codex(
                    root,
                    args.client,
                    args.session,
                    args.provider or "unknown",
                    args.billing or "unknown",
                    args.timeout_ms,
                )
            else:
                discovered = discover_claude(
                    root,
                    args.session,
                    picker=picker,
                    client_version=client_version or "unknown",
                    billing_context=args.billing or "unknown",
                )
            result = discovered.pop("catalog")
            extra = {
                "models": [m["id"] for m in result["models"]],
                "provenance": result["provenance"]["kind"],
                **discovered,
            }
        else:
            result = validate_document(root, "catalog", catalog_template(**ctx))
    elif command == "validate":
        if args.kind not in ("catalog", "request", "plan", "observation"):
            raise FactoryError("models validate requires --kind catalog, request, plan or observation")
        value = read_input("input")
        validate_plan(root, value) if args.kind == "plan" else validate_document(root, args.kind, value)
        result = {
            "valid": True,
            "kind": args.kind,
            "provenance": _provenance(args.kind, value),
            **({"catalog_provenance": value["catalog"]["provenance"]["kind"]} if args.kind == "plan" else {}),
            "inference_tested": False,
        }
    elif command == "plan":
        from .routing import route_model_plan

        request, catalog = read_input("input"), read_input("catalog")
        if isinstance(catalog, dict) and catalog.get("models") == []:
            raise FactoryError(EMPTY_CATALOG_MESSAGE, 2)
        result = route_model_plan(
            root,
            request,
            catalog,
            # Files are re-read around each provider request; stdin cannot change.
            input_paths={
                "request": None if args.input == "-" else args.input,
                "catalog": None if args.catalog == "-" else args.catalog,
            },
        )
        warnings = plan_warnings(result)
        if warnings:
            extra = {"warnings": warnings}
    elif command == "dispatch":
        if not args.assignment:
            raise FactoryError(
                "models dispatch is missing required --assignment (a plan assignment ID such as implementer)"
            )
        result = dispatch_assignment(root, read_input("plan"), args.assignment, read_input("catalog"))
    elif command == "outcome-template":
        result = outcome_template()
    elif command == "outcome-record":
        result = record_model_outcome(root, read_input("input"))
    elif command == "calibration":
        result = model_calibration(root)
    else:
        raise FactoryError(f"Unknown models command: {command}")
    if args.output:
        validate_output()
        write_json(root, args.output, result)
        return {
            "written": args.output,
            "kind": result.get("kind", command),
            "applied": False,
            **extra,
        }
    if extra:
        # Standard output stays a valid catalog/plan document; diagnostics go to stderr.
        print(json.dumps(extra, ensure_ascii=False), file=sys.stderr)
    return result


MODEL_COMMANDS = {
    "sources": "List reviewed model guidance and policy (default)",
    "template": "Print a catalog or request template (--kind catalog|request)",
    "discover": "Build a catalog: Codex app-server, Claude /model picker or guidance, Copilot template",
    "validate": "Validate a catalog, request, plan or observation JSON file (- reads stdin)",
    "plan": "Choose models for a request from a catalog (JEV when jev.enabled, else factory-models)",
    "dispatch": "Revalidate one plan assignment and print native settings to apply (applied: false)",
    "outcome-template": "Print a model outcome record template",
    "outcome-record": "Record an immutable local model outcome from --input",
    "calibration": "Summarize recorded model outcomes",
}
MODEL_OPTIONS = (
    ("profile", "NAME", "Client profile: claude, codex or copilot (default: the project's only profile)"),
    (
        "harness",
        "NAME",
        "codex-native, claude-code-native, copilot-local or copilot-agent-host (claude/codex: derived)",
    ),
    ("session", "LABEL", "Non-sensitive session label such as WORK-20260926-A (template, discover)"),
    ("objective", "TEXT", "Task objective (template --kind request); JEV planning sends it to TypeSafe"),
    ("id", "ID", "Plan ID such as PLAN-001 (template --kind request)"),
    ("kind", "KIND", "template: catalog or request; validate: catalog, request, plan or observation"),
    ("input", "PATH", "JSON to validate, request to plan, or outcome to record; - reads stdin"),
    ("catalog", "PATH", "Model catalog JSON (plan, dispatch); - reads stdin"),
    ("plan", "PATH", "Saved plan JSON (dispatch); - reads stdin"),
    ("assignment", "ID", "Plan assignment to dispatch, such as implementer"),
    ("client", "PATH", "Absolute path of the Codex client binary (discover --profile codex)"),
    ("provider", "NAME", "Provider recorded for Codex models, such as openai (discover)"),
    ("billing", "CONTEXT", "subscription, api, copilot, byok or unknown (discover; default unknown)"),
    ("picker", "TEXT", 'Claude Code /model "Available:" list (discover --profile claude)'),
    ("client-version", "VERSION", "Claude Code version from claude --version (discover --profile claude)"),
    ("output", "PATH", "Write JSON to .factory/local/models/<name>.json instead of printing it"),
)
MODELS_EPILOG = """\
Claude Code example (Claude Code has no machine-readable model list):
  software-factory models discover --profile claude --session WORK-A \\
    --picker "sonnet, opus, haiku, fable" --client-version 2.1.300 \\
    --output .factory/local/models/catalog.json
  software-factory models template --kind request --profile claude --session WORK-A \\
    --id PLAN-001 --objective "Implement the accepted change" \\
    --output .factory/local/models/request.json
  software-factory models plan --input .factory/local/models/request.json \\
    --catalog .factory/local/models/catalog.json --output .factory/local/models/plan.json
  software-factory models validate --kind plan --input .factory/local/models/plan.json

Without --picker, Claude discovery lists reviewed guidance with unknown availability,
which planning rejects. Only models plan calls JEV, and only when jev.enabled is true.
No command changes native model settings. One of --input, --catalog or --plan may be -
to read that JSON (UTF-8, at most 256 KiB) from stdin, for example with <<'JSON'.
"""


def add_parser(subparsers):
    parser = subparsers.add_parser(
        "models",
        help="Discover models and plan assignments; no settings changed",
        description="Discover models and plan assignments; no native settings are changed.\n\nSubcommands:\n"
        + "\n".join(f"  {name:<18}{summary}" for name, summary in MODEL_COMMANDS.items()),
        epilog=MODELS_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "models_command",
        nargs="?",
        choices=list(MODEL_COMMANDS),
        default="sources",
        metavar="<subcommand>",
        help="One of the subcommands listed above (default: sources)",
    )
    for name, metavar, summary in MODEL_OPTIONS:
        parser.add_argument("--" + name, metavar=metavar, help=summary)
    parser.add_argument(
        "--timeout-ms",
        type=int,
        default=15000,
        metavar="MS",
        help="Codex discovery request timeout, 50-60000 (default: 15000)",
    )
    parser.set_defaults(handler=handler)
    return parser
