"""Model planning, metadata protocol and evidence guardrails."""

import argparse
import copy
import io
import json
import sys
import time
import types

import pytest

from software_factory import models
from software_factory.calibration import (
    model_calibration,
    outcome_template,
    record_model_outcome,
    validate_outcome,
)
from software_factory.core import FactoryError, git, now, sha256, write_json


@pytest.fixture
def root(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text(".factory/local/\n")
    return tmp_path


def model(identifier="gpt-6-sol", **patch):
    return {
        "id": identifier,
        "provider": "openai",
        "identity": "exact",
        "resolved_model": None,
        "availability": "visible",
        "operations": ["main", "subagent"],
        "capabilities": ["text", "image", "tools"],
        "context_tokens": 100000,
        "efforts": ["medium", "high"],
        "default_effort": "medium",
        "lifecycle": "stable",
        "cost_tier": 2,
        "guidance_key": None,
        **patch,
    }


def inputs(root, profile="codex", harness="codex-native"):
    policy = models.model_controls(root)["policy"]
    catalog = models.catalog_template(profile=profile, harness=harness, session_id="SESSION-TEST")
    catalog.update(
        client_version="2.1.283",
        billing_context="subscription",
        provenance={"kind": "fixture", "reference": "Synthetic test, no inference"},
        models=[
            model(),
            model("gpt-6-astra", cost_tier=3),
            model("gpt-6-luna", cost_tier=1),
        ],
    )
    request = models.request_template(
        profile=profile,
        harness=harness,
        session_id="SESSION-TEST",
        objective="Bounded task",
        policy=policy,
    )
    request["assignments"] = [a for a in request["assignments"] if a["id"] == "implementer"]
    return request, catalog


def test_plan_selection_explicit_preference_and_tampering(root):
    request, catalog = inputs(root)
    plan = models.create_model_plan(root, request, catalog)
    assert plan["assignments"][0]["model"]["id"] == "gpt-6-sol"
    assert plan["assignments"][0]["fallbacks"] == ["gpt-6-astra"]
    assert models.validate_plan(root, plan) is plan
    plan["assignments"][0]["effort"] = "invented"
    with pytest.raises(FactoryError, match="differs"):
        models.validate_plan(root, plan)
    request["assignments"][0].update(preferred_model="missing", rationale="Explicit task constraint")
    assert models.create_model_plan(root, request, catalog)["assignments"][0]["status"] == "unresolved"


@pytest.mark.parametrize(
    "patch,reason",
    [
        ({"capabilities": ["text"]}, "capability"),
        ({"context_tokens": None}, "context"),
        ({"efforts": ["medium"]}, "effort"),
        ({"cost_tier": None}, "Cost-tier"),
        ({"provider": "unapproved"}, "Provider"),
        ({"lifecycle": "unknown"}, "Stable"),
        ({"availability": "unknown"}, "Availability"),
        ({"identity": "alias", "resolved_model": None}, "Alias"),
    ],
)
def test_hard_constraints_fail_closed(root, patch, reason):
    request, catalog = inputs(root)
    catalog["models"] = [model(**patch)]
    request["assignments"][0]["requirements"].update(
        min_context_tokens=1000,
        effort="high",
        allowed_providers=["openai"],
        max_cost_tier=2,
    )
    assignment = models.create_model_plan(root, request, catalog)["assignments"][0]
    assert assignment["status"] == "unresolved"
    assert reason in " ".join(assignment["rejected"][0]["reasons"])


@pytest.mark.parametrize(
    "patch,reason",
    [
        ({"observed_at": "2020-01-01T00:00:00Z"}, "stale"),
        ({"observed_at": "2099-01-01T00:00:00Z"}, "future"),
        ({"session_id": "DIFFERENT"}, "different session"),
    ],
)
def test_freshness_and_session(root, patch, reason):
    # Intentional change (M-13): a stale, future or foreign-session catalog blocks
    # planning instead of producing an all-unresolved plan with warnings.
    request, catalog = inputs(root)
    catalog.update(patch)
    with pytest.raises(FactoryError, match=f"Cannot plan with this catalog: .*{reason}"):
        models.create_model_plan(root, request, catalog)


def test_invalid_metadata_rejected(root):
    _, catalog = inputs(root)
    for patch in (
        {"token": "forbidden"},
        {"harness": "copilot-local"},
        {"observed_at": "2026-02-30T00:00:00Z"},
        {"models": catalog["models"] * 2},
    ):
        with pytest.raises(FactoryError):
            models.validate_document(root, "catalog", {**catalog, **patch})
    with pytest.raises(FactoryError, match="Unavailable"):
        models.validate_document(
            root,
            "catalog",
            {**catalog, "provenance": {"kind": "unavailable", "reference": "unknown"}},
        )


def test_research_override_cannot_erase_client_prerequisite(root):
    request, catalog = inputs(root, "claude", "claude-code-native")
    catalog["models"] = [model("claude-opus-5-5", provider="anthropic")]
    request["assignments"][0].update(
        preferred_model="claude-opus-5-5", rationale="Explicit reviewed selection"
    )
    controls = models.model_controls(root)
    guidance = next(e for e in controls["recommendations"]["entries"] if "claude-opus-5-5" in e["model_ids"])
    custom = copy.deepcopy(guidance)
    custom.pop("minimum_client_versions", None)
    catalog["client_version"] = "0.0.1"
    # Intentional change (M-1): the reviewed key itself can no longer be re-bound.
    request["research"] = [custom]
    with pytest.raises(FactoryError, match="re-binds reviewed guidance .minimum_client_versions"):
        models.create_model_plan(root, request, catalog)
    # A new research key naming the same model still cannot erase the prerequisite.
    request["research"] = [{**custom, "key": "research-opus"}]
    assignment = models.create_model_plan(root, request, catalog)["assignments"][0]
    assert assignment["status"] == "unresolved"
    assert "Client version" in " ".join(assignment["rejected"][0]["reasons"])


def test_copilot_parent_cost_and_claude_alias_constraints(root):
    request, catalog = inputs(root, "copilot", "copilot-local")
    assert models.create_model_plan(root, request, catalog)["assignments"][0]["status"] == "unresolved"
    catalog["parent_model"] = "gpt-6-sol"
    plan = models.create_model_plan(root, request, catalog)
    assert plan["assignments"][0]["requirements"]["parent_cost_tier"] == 2
    assert models.dispatch_assignment(root, plan, "implementer", catalog)["applied"] is False
    with pytest.raises(FactoryError, match="parent tier"):
        models.dispatch_assignment(root, plan, "implementer", {**catalog, "parent_model": "gpt-6-luna"})
    request, catalog = inputs(root, "claude", "claude-code-native")
    catalog["models"] = [
        model(
            "opus",
            identity="alias",
            provider="anthropic",
            resolved_model="claude-opus-5-5",
        )
    ]
    request["assignments"][0].update(
        preferred_model="opus", rationale="Still cannot establish alias identity"
    )
    assignment = models.create_model_plan(root, request, catalog)["assignments"][0]
    assert assignment["status"] == "unresolved"
    assert "full native model ID" in " ".join(assignment["rejected"][0]["reasons"])


def test_dispatch_revalidates_metadata_billing_and_new_session(root):
    request, catalog = inputs(root)
    plan = models.create_model_plan(root, request, catalog)
    assert models.dispatch_assignment(root, plan, "implementer", catalog)["settings"]["model"] == "gpt-6-sol"
    # Intentional change (item 13): the dispatch catalog's session is checked against
    # the plan's instead of being copied over it; a new session plans again.
    with pytest.raises(FactoryError, match="different session"):
        models.dispatch_assignment(root, plan, "implementer", {**catalog, "session_id": "NEW"})
    with pytest.raises(FactoryError, match="billing"):
        models.dispatch_assignment(root, plan, "implementer", {**catalog, "billing_context": "api"})
    changed = copy.deepcopy(catalog)
    changed["models"][0]["context_tokens"] += 1
    with pytest.raises(FactoryError, match="metadata changed"):
        models.dispatch_assignment(root, plan, "implementer", changed)


def bound(root, exact=False):
    request, catalog = inputs(root)
    if exact:
        request["assignments"][0]["requirements"].update(exact_model="gpt-6-sol", effort="high")
    plan = models.create_model_plan(root, request, catalog)
    relative = ".factory/missions/M-TEST/models/PLAN-001.json"
    write_json(root, relative, plan)
    binding = {
        "plan_path": relative,
        "plan_hash": sha256((root / relative).read_bytes()),
        "assignment_id": "implementer",
        "assignment_hash": models.model_hash(plan["assignments"][0]),
    }
    task = {
        "id": "T-001",
        "attempts": 1,
        "model_assignment": binding,
        "model_attempts": [{"attempt": 1, "assignment_hash": binding["assignment_hash"]}],
    }
    observation = {
        "schema_version": 1,
        "kind": "observation",
        "assignment_hash": binding["assignment_hash"],
        "attempt": 1,
        "requested_model": "gpt-6-sol",
        "requested_effort": plan["assignments"][0]["effort"],
        "observed_model": None,
        "observed_effort": None,
        "provenance": "unknown",
        "reference": None,
        "fallback_reason": None,
        "observed_at": now(),
    }
    return {"id": "M-TEST", "tasks": [task]}, task, observation


def test_bound_observation_requires_runtime_exact_identity_and_attempt(root):
    mission, task, observation = bound(root, exact=True)
    with pytest.raises(FactoryError, match="Exact-model"):
        models.validate_task_observation(root, mission, task, observation)
    observation.update(
        provenance="runtime",
        reference="Synthetic runtime identity",
        observed_model="gpt-6-sol",
        observed_effort="high",
    )
    models.validate_task_observation(root, mission, task, observation)
    with pytest.raises(FactoryError, match="current task attempt"):
        models.validate_task_observation(root, mission, task, {**observation, "attempt": 2})
    observation["observed_effort"] = "medium"
    with pytest.raises(FactoryError, match="effort requirement"):
        models.validate_task_observation(root, mission, task, observation)


def test_observation_fallback_and_immutable_binding(root):
    mission, task, observation = bound(root)
    observation.update(provenance="runtime", reference="Fixture", observed_model="gpt-6-astra")
    with pytest.raises(FactoryError, match="recorded reason"):
        models.validate_task_observation(root, mission, task, observation)
    observation["fallback_reason"] = "Permitted fallback fixture"
    models.validate_task_observation(root, mission, task, observation)
    observation["observed_model"] = "gpt-6-luna"
    with pytest.raises(FactoryError, match="eligible recorded fallback"):
        models.validate_task_observation(root, mission, task, observation)
    (root / task["model_assignment"]["plan_path"]).write_text("{}")
    with pytest.raises(FactoryError, match="hash changed"):
        models.resolve_assignment(root, mission, task["model_assignment"])


def fake_client(root, behavior):
    path = root / "fake-client"
    path.write_text(
        f"#!{sys.executable}\nimport sys,json,time\nfor line in sys.stdin:\n q=json.loads(line)\n with open('methods.log','a') as log: log.write(q['method']+'\\n')\n if q['method']=='initialized': continue\n if q['method']=='initialize':\n  print(json.dumps({{'id':q['id'],'result':{{'userAgent':'synthetic-1.2.3'}}}}),flush=True); continue\n {behavior}\n"
    )
    path.chmod(0o755)
    return path


def test_discovery_is_metadata_only_and_unknown_fields_remain_unknown(root):
    binary = fake_client(
        root,
        "print(json.dumps({'id':q['id'],'result':{'data':[{'model':'first' if 'cursor' not in q['params'] else 'second'}],'nextCursor':'next' if 'cursor' not in q['params'] else None}}),flush=True)",
    )
    discovered = models.discover_codex(root, binary, "TEST", provider="openai", timeout_ms=1000)
    catalog = discovered["catalog"]
    assert discovered["warnings"] == [models.CODEX_ENRICHMENT_WARNING]
    assert [m["id"] for m in catalog["models"]] == ["first", "second"]
    assert catalog["models"][0]["capabilities"] == []
    assert catalog["models"][0]["operations"] == ["main"]
    assert catalog["models"][0]["lifecycle"] == "unknown"
    assert (root / "methods.log").read_text().splitlines() == [
        "initialize",
        "initialized",
        "model/list",
        "model/list",
    ]


@pytest.mark.parametrize(
    "behavior,reason",
    [
        (
            "print(json.dumps({'id':q['id'],'result':{'data':[],'nextCursor':'again'}}),flush=True)",
            "pagination",
        ),
        ("time.sleep(5)", "timeout|deadline"),
        ("print('x' * (4 * 1024 * 1024 + 1),flush=True)", "size limit"),
        (
            "print(json.dumps({'id':q['id'],'error':{'private':'not surfaced'}}),flush=True)",
            "request failed",
        ),
    ],
)
def test_discovery_bounds_failures(root, behavior, reason):
    with pytest.raises(FactoryError, match=reason):
        models.discover_codex(root, fake_client(root, behavior), "TEST", timeout_ms=250)


def execution_fixture(root, monkeypatch):
    mission, task, observation = bound(root)
    observation.update(
        provenance="runtime",
        reference="Synthetic identity",
        observed_model="gpt-6-sol",
        observed_effort="medium",
    )
    result = {
        "schema_version": 1,
        "mission_id": mission["id"],
        "task_id": task["id"],
        "fingerprint": "a" * 64,
        "status": "blocked",
        "summary": "Synthetic unsuccessful result",
        "changed_files": [],
        "checks": [],
        "evidence": [],
        "unresolved": ["Fixture incomplete"],
        "created_at": now(),
        "execution_attempt": 1,
        "model_observation": observation,
    }
    relative = ".factory/missions/M-TEST/results/records/T-001-fixture.json"
    write_json(root, relative, result)
    module = types.ModuleType("software_factory.workflow")
    module.load_mission = lambda *_: mission
    module.result_index = lambda *_: {
        "records": {"T-001": {"file": "T-001-fixture.json", "sha256": sha256((root / relative).read_bytes())}}
    }
    module.load_result = lambda *_, **_kwargs: result
    monkeypatch.setitem(sys.modules, "software_factory.workflow", module)
    value = outcome_template()
    value.update(
        mission_id=mission["id"],
        execution_context={
            "client_version": "fixture",
            "reference": "Execution fixture",
        },
        comparison={
            "prompt_hash": "a" * 64,
            "tools_hash": "b" * 64,
            "acceptance_hash": "c" * 64,
        },
        measurements={
            "accepted": False,
            "wall_time_ms": 1200,
            "missed_defects": 2,
            "usage": {"unit": "tokens", "amount": 500},
        },
        provenance={"kind": "user_report", "reference": "Synthetic review"},
    )
    return value, result, mission


def test_outcomes_immutable_tamper_detection_and_execution_dedup(root, monkeypatch):
    value, _, _ = execution_fixture(root, monkeypatch)
    saved = record_model_outcome(root, value)
    assert saved["model_attributed"] is True
    with pytest.raises(FactoryError, match="already recorded.*immutable"):
        record_model_outcome(root, value)
    with pytest.raises(FactoryError, match="attempt 1 already has outcome OUTCOME-001"):
        record_model_outcome(root, {**value, "id": "OUTCOME-002"})
    report = model_calibration(root)
    assert report["records"] == 1
    row = report["cohorts"][0]["models"][0]
    assert row["accepted"] == {"samples": 1, "count": 0, "rate": 0}
    assert row["wall_time_ms"]["median"] == 1200
    assert "ranking" not in report
    record = json.loads((root / saved["recorded"]).read_text())
    duplicate = {**record, "id": "OUTCOME-DUP"}
    duplicate["content_hash"] = models.model_hash({k: v for k, v in duplicate.items() if k != "content_hash"})
    write_json(root, ".factory/local/models/outcomes/OUTCOME-DUP.json", duplicate)
    assert "Duplicate execution" in model_calibration(root)["invalid"][0]["reason"]
    record["measurements"]["accepted"] = True
    write_json(root, saved["recorded"], record)
    assert any("hash changed" in row["reason"] for row in model_calibration(root)["invalid"])


def test_outcome_guards_reject_unproven_metrics_stale_attempt_and_sensitive_fields(root, monkeypatch):
    value, result, mission = execution_fixture(root, monkeypatch)
    with pytest.raises(FactoryError, match="completed result"):
        record_model_outcome(root, {**value, "measurements": {**value["measurements"], "accepted": True}})
    for patch, reason in [
        ({"transcript": "no"}, "Invalid model-outcome"),
        ({"observed_at": "2026-02-30T00:00:00.000Z"}, "real"),
        ({"provenance": {"kind": "unknown", "reference": None}}, "provenance"),
    ]:
        with pytest.raises(FactoryError, match=reason):
            validate_outcome(root, {**value, **patch})
    mission["tasks"][0]["attempts"] = 2
    with pytest.raises(FactoryError, match="current task attempt"):
        record_model_outcome(root, value)
    mission["tasks"][0].pop("model_assignment")
    result.pop("model_observation")
    result.pop("execution_attempt")
    write_json(root, ".factory/missions/M-TEST/results/records/T-001-fixture.json", result)
    with pytest.raises(FactoryError, match="no execution attempt"):
        record_model_outcome(root, value)


def test_privacy_rejects_directory_negation_and_tracked_records(root, monkeypatch):
    value, _, _ = execution_fixture(root, monkeypatch)
    (root / ".gitignore").write_text(".factory/local/\n!.factory/local/\n")
    with pytest.raises(FactoryError, match="ignore"):
        record_model_outcome(root, value)
    (root / ".gitignore").write_text(".factory/local/\n")
    saved = record_model_outcome(root, value)
    git(root, "add", "-f", saved["recorded"])
    with pytest.raises(FactoryError, match="tracked"):
        record_model_outcome(root, {**value, "id": "NEXT"})


def test_calibration_unknown_identity_and_units_remain_separate(root):
    for index, (unit, identity) in enumerate(
        [("tokens", "runtime"), ("credits", "runtime"), ("tokens", "unknown")]
    ):
        record = outcome_template()
        record.update(
            kind="outcome",
            id=f"OUTCOME-{index}",
            mission_id=f"M-{index}",
            recorded_at=now(),
            comparison={
                "prompt_hash": "a" * 64,
                "tools_hash": "b" * 64,
                "acceptance_hash": "c" * 64,
            },
            execution_context={"client_version": "fixture", "reference": "Fixture"},
            measurements={
                "accepted": None,
                "wall_time_ms": None,
                "missed_defects": None,
                "usage": {"unit": unit, "amount": 1},
            },
            provenance={"kind": "user_report", "reference": "Fixture"},
            result={
                "path": f".factory/missions/M-{index}/results/records/T-001-fixture.json",
                "sha256": "a" * 64,
                "fingerprint": "b" * 64,
                "status": "blocked",
                "attempt": 1,
                "observed_model": "gpt-6-sol" if identity == "runtime" else None,
                "observed_effort": None,
                "identity_provenance": identity,
                "identity_reference": "Fixture" if identity == "runtime" else None,
                "profile": "codex",
                "harness": "codex-native",
                "catalog_client_version": "fixture",
                "billing_context": "subscription",
            },
        )
        record["content_hash"] = models.model_hash(record)
        write_json(root, f".factory/local/models/outcomes/{record['id']}.json", record)
    report = model_calibration(root)
    assert report["records"] == 3 and len(report["unattributed"]) == 1
    row = report["cohorts"][0]["models"][0]
    assert row["accepted"]["samples"] == 0
    assert [u["unit"] for u in row["usage"]] == ["credits", "tokens"]
    assert [u["samples"] for u in row["usage"]] == [1, 1]


def test_parser_registers_all_model_commands():
    parser = argparse.ArgumentParser()
    models.add_parser(parser.add_subparsers())
    args = parser.parse_args(
        [
            "models",
            "dispatch",
            "--plan",
            "p.json",
            "--assignment",
            "implementer",
            "--catalog",
            "c.json",
        ]
    )
    assert args.handler == models.handler
    assert args.models_command == "dispatch"


def test_discovery_write_backpressure_respects_total_deadline(root):
    binary = fake_client(
        root,
        "print(json.dumps({'id':q['id'],'result':{'data':[], 'nextCursor':'x'*131072}}),flush=True); time.sleep(1)",
    )
    started = time.monotonic()
    with pytest.raises(FactoryError, match="write timeout|deadline"):
        models.discover_codex(root, binary, "TEST", timeout_ms=100)
    assert time.monotonic() - started < 0.75


def claude_project(root, jev=False):
    write_json(
        root,
        "factory.json",
        {
            "schema_version": 1,
            "name": "claude-models",
            "profile": "claude",
            "completion_target": "READY_PR",
            "work_types": ["maintenance"],
            "limits": {"repair_attempts": 3, "parallel_writers": 1, "check_timeout_seconds": 30},
            "checks": [
                {
                    "id": "unit",
                    "command": [sys.executable, "-c", "pass"],
                    "cwd": ".",
                    "required": True,
                    "timeout_seconds": 30,
                }
            ],
            "owners": {"maintainer": None, "reviewer": None},
            "delivery": {
                "enabled": False,
                "staging_command": None,
                "release_command": None,
                "recovery_command": None,
            },
            "evidence_exclude": [".factory/local/", ".factory/missions/"],
            "jev": {"enabled": jev, "provider": "typesafe", "model": "jev-1.13.0"},
        },
    )
    return root


def run_models(root, *argv):
    parser = argparse.ArgumentParser()
    models.add_parser(parser.add_subparsers())
    args = parser.parse_args(["models", *argv])
    args.root = root
    return args.handler(args)


CLAUDE_IDS = ["claude-opus-5-5", "claude-sonnet-5", "claude-haiku-4-5", "claude-fable-5-1"]
CATALOG = ".factory/local/models/catalog.json"
REQUEST = ".factory/local/models/request.json"
PLAN = ".factory/local/models/plan.json"
USER_PICKER = (
    "Available: sonnet, opus, haiku, fable, best, sonnet[1m], opus[1m], fable[1m], opusplan, default, "
    "or a full model ID."
)


def discover_claude_catalog(root, *extra):
    return run_models(
        root, "discover", "--profile", "claude", "--session", "WORK-A", *extra, "--output", CATALOG
    )


def claude_request(root, *assignments):
    run_models(
        root,
        *("template", "--kind", "request", "--profile", "claude", "--session", "WORK-A"),
        *("--id", "PLAN-001", "--objective", "Implement the accepted change", "--output", REQUEST),
    )
    if assignments:
        request = json.loads((root / REQUEST).read_text())
        request["assignments"] = [a for a in request["assignments"] if a["id"] in assignments]
        write_json(root, REQUEST, request)


def test_claude_discovery_without_picker_lists_unverified_guidance(root, capsys):
    claude_project(root)
    summary = discover_claude_catalog(root)
    assert summary["models"] == CLAUDE_IDS
    assert summary["provenance"] == "fixture"
    assert summary["skipped"] == [] and "--picker" in summary["warnings"][0]
    catalog = json.loads((root / CATALOG).read_text())
    assert "availability not verified for this account" in catalog["provenance"]["reference"]
    assert "/model picker" in catalog["provenance"]["reference"]
    assert {m["availability"] for m in catalog["models"]} == {"unknown"}
    assert [m["guidance_key"] for m in catalog["models"]] == [
        "anthropic-opus",
        "anthropic-sonnet",
        "anthropic-haiku",
        "anthropic-fable",
    ]
    assert (catalog["client_version"], catalog["billing_context"]) == ("unknown", "unknown")
    # Without --output the catalog stays clean on stdout and diagnostics go to stderr.
    printed = run_models(root, "discover", "--profile", "claude", "--session", "WORK-A")
    models.validate_document(root, "catalog", printed)
    assert "--picker" in json.loads(capsys.readouterr().err)["warnings"][0]


def test_claude_discovery_with_picker_keeps_listed_models(root):
    claude_project(root)
    summary = discover_claude_catalog(root, "--picker", "sonnet, opus, haiku", "--billing", "subscription")
    assert summary["models"] == ["claude-sonnet-5", "claude-opus-5-5", "claude-haiku-4-5"]
    assert summary["provenance"] == "user_report" and summary["skipped"] == []
    # Opus needs a documented minimum client version, which an unknown version cannot show.
    assert any("claude-opus-5-5" in w and "--client-version" in w for w in summary["warnings"])
    catalog = json.loads((root / CATALOG).read_text())
    assert {m["availability"] for m in catalog["models"]} == {"visible"}
    assert '"sonnet, opus, haiku"' in catalog["provenance"]["reference"]
    assert catalog["billing_context"] == "subscription" and catalog["client_version"] == "unknown"


def test_claude_picker_reports_skipped_entries_and_extended_context(root):
    claude_project(root)
    summary = discover_claude_catalog(
        root,
        *("--picker", USER_PICKER + ", mythos, claude-sonnet-4-5, claude-haiku-4-5"),
        *("--client-version", "2.1.300"),
    )
    assert summary["models"] == ["claude-sonnet-5", "claude-opus-5-5", "claude-haiku-4-5", "claude-fable-5-1"]
    skipped = {s["entry"]: s["reason"] for s in summary["skipped"]}
    assert set(skipped) == {"best", "opusplan", "default", "mythos", "claude-sonnet-4-5"}
    assert "strategy" in skipped["best"] and "No reviewed factory guidance" in skipped["mythos"]
    assert len(summary["notes"]) == 3 and all("context" in note for note in summary["notes"])
    assert summary["warnings"] == []
    catalog = json.loads((root / CATALOG).read_text())
    assert catalog["client_version"] == "2.1.300"
    assert all(m["context_tokens"] is None for m in catalog["models"])
    with pytest.raises(FactoryError, match="No --picker entry matched.*best, mythos"):
        discover_claude_catalog(root, "--picker", "best, mythos")
    with pytest.raises(FactoryError, match="--picker and --client-version apply only"):
        run_models(root, "discover", "--profile", "codex", "--session", "WORK-A", "--picker", "sonnet")


def test_plan_with_empty_catalog_fails_fast_before_selection(root, monkeypatch):
    claude_project(root, jev=True)
    claude_request(root)
    run_models(
        root,
        "template",
        "--kind",
        "catalog",
        "--profile",
        "claude",
        "--session",
        "WORK-A",
        "--output",
        CATALOG,
    )
    monkeypatch.setattr(
        "software_factory.routing.route_model_plan", lambda *_a, **_k: pytest.fail("routing reached")
    )
    with pytest.raises(FactoryError) as raised:
        run_models(root, "plan", "--input", REQUEST, "--catalog", CATALOG)
    assert raised.value.exit_code == 2
    assert str(raised.value) == models.EMPTY_CATALOG_MESSAGE
    assert "models discover --profile claude --picker" in str(raised.value)
    assert "can only choose among listed models" in str(raised.value)


def test_plan_explains_when_no_assignment_has_candidates(root):
    claude_project(root)
    claude_request(root)
    discover_claude_catalog(root)
    summary = run_models(root, "plan", "--input", REQUEST, "--catalog", CATALOG, "--output", PLAN)
    assert summary["written"] == PLAN
    plan = json.loads((root / PLAN).read_text())
    assert {a["status"] for a in plan["assignments"]} == {"unresolved"}
    assert "no eligible candidates" in summary["warnings"][0]
    assert "Availability is unknown (" in summary["warnings"][0]


def test_plan_selects_from_picker_catalog_with_jev_off(root):
    claude_project(root)
    claude_request(root)
    discover_claude_catalog(root, "--picker", "sonnet, opus, haiku", "--client-version", "2.1.300")
    summary = run_models(root, "plan", "--input", REQUEST, "--catalog", CATALOG, "--output", PLAN)
    assert "warnings" not in summary
    plan = json.loads((root / PLAN).read_text())
    selected = {a["id"]: a["model"]["id"] for a in plan["assignments"] if a["status"] == "selected"}
    assert selected == {
        "orchestrator": "claude-opus-5-5",
        "planner": "claude-opus-5-5",
        "implementer": "claude-sonnet-5",
        "verifier": "claude-haiku-4-5",
        "reviewer": "claude-opus-5-5",
    }
    assert plan["routing"]["engine"] == "factory-models" and plan["routing"]["provider_requests"] == 0
    models.validate_plan(root, plan)


def test_plan_calls_jev_for_picker_catalog_when_enabled(root, monkeypatch):
    from software_factory import routing

    claude_project(root, jev=True)
    claude_request(root, "implementer")
    discover_claude_catalog(root, "--picker", "sonnet, opus, haiku", "--client-version", "2.1.300")
    calls = []

    def choose(body, **_kwargs):
        calls.append([c["id"] for c in body["state"]["candidates"]])
        key = next(c["choice"] for c in body["state"]["candidates"] if c["id"] == "claude-sonnet-5")
        criteria = body["questions"]["selection"]["criteria"]
        return {
            "model": body["model"],
            "answers": {
                "selection": {
                    "type": "choice",
                    "choice": key,
                    "confidence": 0.9,
                    "probabilities": {name: 1 if name == key else 0 for name in criteria},
                }
            },
            "usage": {"input_tokens": 100, "output_tokens": 1},
        }

    original = routing.route_model_plan
    monkeypatch.setattr(
        routing,
        "route_model_plan",
        lambda *a, **k: original(*a, get_api_key=lambda: "synthetic-key", transport=choose, **k),
    )
    run_models(root, "plan", "--input", REQUEST, "--catalog", CATALOG, "--output", PLAN)
    plan = json.loads((root / PLAN).read_text())
    assert calls == [["claude-haiku-4-5", "claude-opus-5-5", "claude-sonnet-5"]]
    assert plan["routing"]["engine"] == "jev" and plan["routing"]["provider_requests"] == 1
    assert plan["assignments"][0]["model"]["id"] == "claude-sonnet-5"


def test_model_command_errors_name_the_flag_to_fix(root):
    claude_project(root)
    base = ("template", "--kind", "request", "--profile", "claude")
    with pytest.raises(FactoryError, match=r"missing required --session .*, --id .*, --objective"):
        run_models(root, *base)
    with pytest.raises(FactoryError, match=r"missing required --id \(a plan ID") as raised:
        run_models(root, *base, "--session", "WORK-A", "--objective", "Change")
    assert "None is not of type" not in str(raised.value)
    with pytest.raises(FactoryError, match="--kind catalog or --kind request"):
        run_models(root, "template", "--profile", "claude", "--session", "WORK-A")
    with pytest.raises(
        FactoryError, match="Invalid --harness 'codex-native' for profile claude; use claude-code-native"
    ):
        run_models(
            root, "discover", "--profile", "claude", "--harness", "codex-native", "--session", "WORK-A"
        )
    with pytest.raises(FactoryError, match="Invalid --profile 'gemini'; use one of: codex, claude, copilot"):
        run_models(root, "sources", "--profile", "gemini")
    with pytest.raises(FactoryError, match=r"use \.factory/local/models/<name>\.json"):
        run_models(root, "sources", "--output", "models.json")
    with pytest.raises(FactoryError, match="Invalid --billing 'team'; use one of: subscription"):
        discover_claude_catalog(root, "--billing", "team")


# 0.3.2 hardening (WP5): models, discovery, parser and calibration fixes.


def stdin_bytes(monkeypatch, data):
    monkeypatch.setattr(sys, "stdin", types.SimpleNamespace(buffer=io.BytesIO(data)))


def test_models_inputs_read_one_json_document_from_stdin(root, monkeypatch):
    claude_project(root)
    request, catalog = inputs(root, "claude", "claude-code-native")
    catalog["models"] = [model("claude-sonnet-5", provider="anthropic")]
    request["session_id"] = catalog["session_id"]
    write_json(root, CATALOG, catalog)
    stdin_bytes(monkeypatch, json.dumps(catalog).encode())
    assert run_models(root, "validate", "--kind", "catalog", "--input", "-")["valid"] is True
    stdin_bytes(monkeypatch, json.dumps(request).encode())
    plan = run_models(root, "plan", "--input", "-", "--catalog", CATALOG)
    assert plan["assignments"][0]["model"]["id"] == "claude-sonnet-5"
    write_json(root, PLAN, plan)
    stdin_bytes(monkeypatch, json.dumps(catalog).encode())
    dispatched = run_models(root, "dispatch", "--plan", PLAN, "--assignment", "implementer", "--catalog", "-")
    assert dispatched["settings"]["model"] == "claude-sonnet-5"
    stdin_bytes(monkeypatch, json.dumps(plan).encode())
    assert run_models(root, "validate", "--kind", "plan", "--input", "-")["kind"] == "plan"


def test_models_stdin_is_bounded_single_and_strict(root, monkeypatch):
    claude_project(root)
    with pytest.raises(FactoryError, match="Only one input can read stdin; --input and --catalog"):
        run_models(root, "plan", "--input", "-", "--catalog", "-")
    with pytest.raises(FactoryError, match="Only one input can read stdin"):
        run_models(root, "dispatch", "--plan", "-", "--catalog", "-", "--assignment", "implementer")
    for data, reason in (
        (b" " * (256 * 1024 + 1), "exceeds 256 KiB"),
        (b"\xff{}", "not valid UTF-8"),
        (b"", "empty"),
        (b'{"a": NaN}', "Cannot read --input JSON from stdin"),
    ):
        stdin_bytes(monkeypatch, data)
        with pytest.raises(FactoryError, match=reason):
            run_models(root, "validate", "--kind", "catalog", "--input", "-")
    stdin_bytes(monkeypatch, b"{}")
    with pytest.raises(FactoryError, match="Invalid model-outcome"):
        run_models(root, "outcome-record", "--input", "-")


@pytest.mark.parametrize(
    "version,ok",
    [("v2.1.300", True), ("2.1.300 (Claude Code)", True), ("V2.1.280", True), ("2.1.279", False)],
)
def test_client_version_accepts_v_prefix(version, ok):
    guidance = {"minimum_client_versions": {"claude-code-native": "2.1.280"}}
    catalog = {"harness": "claude-code-native", "client_version": version}
    assert (models._version_reasons(guidance, catalog) == []) is ok


def test_picker_parses_prefixes_parentheticals_versions_and_space_lists(root):
    entries = models._claude_guidance(models.model_controls(root)["recommendations"]["entries"])
    matched, skipped, _ = models.parse_claude_picker(
        "Available models: Default (recommended), Opus (claude-opus-5-5), Sonnet 5 (1M context), "
        "Claude Haiku 4.5, Fable v5.1, ()",
        entries,
    )
    assert [e["model_ids"][0] for e in matched] == [
        "claude-opus-5-5",
        "claude-sonnet-5",
        "claude-haiku-4-5",
        "claude-fable-5-1",
    ]
    assert skipped == [
        {"entry": "Default", "reason": "Selection strategy or default alias, not a single model"}
    ]
    matched, skipped, notes = models.parse_claude_picker("sonnet opus[1m] haiku mythos", entries)
    assert [e["key"] for e in matched] == ["anthropic-sonnet", "anthropic-opus", "anthropic-haiku"]
    assert [s["entry"] for s in skipped] == ["mythos"] and len(notes) == 1
    # A version that names no reviewed model is not silently mapped to its family.
    matched, skipped, _ = models.parse_claude_picker("Sonnet 4.5", entries)
    assert matched == [] and [s["entry"] for s in skipped] == ["Sonnet 4.5"]


def test_picker_error_has_no_empty_parentheses(root):
    claude_project(root)
    with pytest.raises(FactoryError) as raised:
        discover_claude_catalog(root, "--picker", "a full model ID")
    assert "()" not in str(raised.value) and "matched reviewed factory guidance;" in str(raised.value)


def test_resolve_assignment_and_bound_observation_errors_are_named(root):
    mission, task, _ = bound(root)
    binding = task["model_assignment"]
    with pytest.raises(FactoryError, match="needs a model_observation for attempt 1"):
        models.validate_task_observation(root, mission, task, None)
    (root / binding["plan_path"]).write_bytes(b"{not json")
    binding["plan_hash"] = sha256(b"{not json")
    with pytest.raises(FactoryError, match="Cannot read model plan"):
        models.resolve_assignment(root, mission, binding)
    (root / binding["plan_path"]).unlink()
    (root / binding["plan_path"]).mkdir()
    with pytest.raises(FactoryError, match="Cannot read model plan"):
        models.resolve_assignment(root, mission, binding)


def test_sources_and_template_work_without_factory_json(root):
    assert not (root / "factory.json").exists()
    assert run_models(root, "sources", "--profile", "codex")["entries"]
    template = run_models(root, "template", "--kind", "catalog", "--profile", "codex", "--session", "S1")
    assert template["kind"] == "catalog"
    with pytest.raises(FactoryError, match=r"missing required --profile \(claude, codex or copilot\)$"):
        run_models(root, "template", "--kind", "catalog", "--session", "S1")


def test_cli_validates_billing_timeout_assignment_and_profile_order(root):
    fail = root / "must-not-run"
    for argv, reason in (
        (("--profile", "codex", "--client", str(fail), "--billing", "team"), "Invalid --billing 'team'"),
        (("--profile", "claude", "--timeout-ms", "10"), "Invalid --timeout-ms 10; use 50-60000"),
        (("--profile", "copilot", "--harness", "copilot-local", "--timeout-ms", "70000"), "--timeout-ms"),
    ):
        with pytest.raises(FactoryError, match=reason):
            run_models(root, "discover", "--session", "S1", *argv)
    with pytest.raises(FactoryError, match="missing required --assignment"):
        run_models(root, "dispatch", "--plan", "p.json", "--catalog", "c.json")
    request, catalog = inputs(root)
    plan = models.create_model_plan(root, request, catalog)
    with pytest.raises(
        FactoryError, match="Assignment 'reviewer' is missing from the plan; use one of: implementer"
    ):
        models.dispatch_assignment(root, plan, "reviewer", catalog)
    claude_project(root)
    config = json.loads((root / "factory.json").read_text())
    config["profile"] = ["claude", "codex"]
    write_json(root, "factory.json", config)
    with pytest.raises(FactoryError) as raised:
        run_models(root, "discover", "--session", "S1", "--picker", "sonnet")
    assert str(raised.value) == (
        "models discover is missing required --profile (one of this project's profiles: claude, codex)"
    )


def test_validation_errors_name_the_json_path_and_provenance_is_per_kind(root, monkeypatch):
    request, catalog = inputs(root)
    catalog["models"][1]["capabilities"] = ["text", "audio"]
    with pytest.raises(FactoryError, match="Invalid model catalog at models/1/capabilities/1: 'audio'"):
        models.validate_document(root, "catalog", catalog)
    catalog["models"][1]["capabilities"] = ["text"]
    claude_project(root)
    write_json(root, CATALOG, catalog)
    assert run_models(root, "validate", "--kind", "catalog", "--input", CATALOG)["provenance"] == "fixture"
    _, _, observation = bound(root)
    write_json(root, ".factory/local/models/observation.json", observation)
    result = run_models(
        root, "validate", "--kind", "observation", "--input", ".factory/local/models/observation.json"
    )
    assert result["provenance"] == "unknown"
    write_json(root, REQUEST, request)
    assert run_models(root, "validate", "--kind", "request", "--input", REQUEST)["provenance"] == (
        "local-unattested"
    )


def auto_catalog(root, lifecycle):
    request, catalog = inputs(root, "copilot", "copilot-agent-host")
    catalog["models"] = [
        model(
            "Auto",
            identity="auto",
            provider="github",
            lifecycle=lifecycle,
            context_tokens=None,
            cost_tier=None,
        )
    ]
    request["assignments"][0].update(preferred_model="Auto", rationale="Copilot auto selection by the user")
    return request, catalog


def test_auto_identity_follows_its_own_lifecycle_under_default_policy(root):
    request, catalog = auto_catalog(root, "stable")
    assignment = models.create_model_plan(root, request, catalog)["assignments"][0]
    assert assignment["status"] == "selected" and assignment["model"]["id"] == "Auto"
    request, catalog = auto_catalog(root, "preview")
    assignment = models.create_model_plan(root, request, catalog)["assignments"][0]
    assert assignment["status"] == "unresolved"
    reasons = " ".join(assignment["rejected"][0]["reasons"])
    assert "Stable model required" in reasons and "Auto cannot prove" not in reasons


def test_display_names_match_copilot_only_and_are_never_native_ids(root):
    request, catalog = inputs(root)
    catalog["models"] = [model("GPT-6 Sol")]
    assignment = models.create_model_plan(root, request, catalog)["assignments"][0]
    assert assignment["status"] == "unresolved"
    assert "No reviewed task-fit guidance" in " ".join(assignment["rejected"][0]["reasons"])
    request, catalog = inputs(root, "copilot", "copilot-agent-host")
    catalog["models"] = [model("GPT-6 Sol")]
    assignment = models.create_model_plan(root, request, catalog)["assignments"][0]
    assert assignment["model"]["id"] == "GPT-6 Sol" and assignment["sources"]
    controls = models.model_controls(root)
    assert all(" " not in i for e in controls["recommendations"]["entries"] for i in e["model_ids"])
    entry = copy.deepcopy(controls["recommendations"]["entries"][0])
    entry.update(key="dup-names", display_names=[entry["model_ids"][0].upper()])
    request["research"] = [entry]
    with pytest.raises(FactoryError, match="repeats a model ID or display name"):
        models.validate_document(root, "request", request)


def test_self_resolving_entries_and_free_intents_are_rejected(root):
    request, catalog = inputs(root)
    for patch, reason in (
        ({"resolved_model": "gpt-6-sol"}, "Only aliases record resolved_model"),
        ({"identity": "alias", "resolved_model": "gpt-6-sol"}, "cannot resolve to itself"),
    ):
        catalog["models"][0] = model(**patch)
        with pytest.raises(FactoryError, match=reason):
            models.validate_document(root, "catalog", catalog)
    request, catalog = inputs(root)
    request["assignments"][0]["intent"] = "review"
    with pytest.raises(FactoryError, match="intent review differs from the policy intent coding"):
        models.create_model_plan(root, request, catalog)


def test_research_may_refresh_only_evidence_of_a_reviewed_key(root):
    request, catalog = inputs(root)
    reviewed = next(
        e for e in models.model_controls(root)["recommendations"]["entries"] if e["key"] == "openai-sol"
    )
    refreshed = {
        **copy.deepcopy(reviewed),
        "strengths": "Refreshed research.",
        "sources": ["https://example.com/sol"],
        "checked_at": catalog["observed_at"],
    }
    request["research"] = [refreshed]
    assignment = models.create_model_plan(root, request, catalog)["assignments"][0]
    assert assignment["sources"] == ["https://example.com/sol"]
    for patch in ({"model_ids": ["gpt-6-luna"]}, {"provider": "other"}, {"profiles": ["codex"]}):
        request["research"] = [{**refreshed, **patch}]
        with pytest.raises(FactoryError, match="re-binds reviewed guidance"):
            models.create_model_plan(root, request, catalog)


def test_codex_discovery_filters_modalities_and_bounds_errors(root, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "synthetic-not-a-secret")
    binary = fake_client(
        root,
        "import os; open('env.log','w').write(str('TYPESAFE_API_KEY' in os.environ)); "
        "print(json.dumps({'id':q['id'],'result':{'data':[{'model':'m1','inputModalities':"
        "['text','audio','image']}],'nextCursor':''}}),flush=True)",
    )
    discovered = models.discover_codex(root, binary, "TEST", timeout_ms=1000)
    assert discovered["catalog"]["models"][0]["capabilities"] == ["text", "image"]
    assert discovered["warnings"][0] == "Ignored input modalities the catalog does not model: audio"
    assert "enriched" in discovered["warnings"][1]
    assert (root / "env.log").read_text() == "False"
    binary = fake_client(root, "print(json.dumps({'id':q['id'],'result':{'data':[{'model':7}]}}),flush=True)")
    with pytest.raises(FactoryError, match="no string model ID"):
        models.discover_codex(root, binary, "TEST", timeout_ms=1000)
    binary = fake_client(
        root, "sys.stderr.write('boom password=hunter2secret\\n' + 'x' * 5000 + 'tail-marker'); sys.exit(3)"
    )
    with pytest.raises(FactoryError, match="exited before completing") as raised:
        models.discover_codex(root, binary, "TEST", timeout_ms=1000)
    message = str(raised.value)
    assert "client stderr tail:" in message and message.endswith("tail-marker")
    assert len(message) < 1300 and "hunter2" not in message


def test_codex_cli_reports_discovery_warnings(root, monkeypatch, capsys):
    binary = fake_client(
        root,
        "print(json.dumps({'id':q['id'],'result':{'data':[{'model':'m1'}],'nextCursor':None}}),flush=True)",
    )
    result = run_models(root, "discover", "--profile", "codex", "--session", "S1", "--client", str(binary))
    assert [m["id"] for m in result["models"]] == ["m1"]
    diagnostics = json.loads(capsys.readouterr().err)
    assert diagnostics["provenance"] == "runtime" and "enriched" in diagnostics["warnings"][0]


def test_calibration_path_must_be_a_directory(root, monkeypatch):
    value, _, _ = execution_fixture(root, monkeypatch)
    outcomes = root / ".factory/local/models/outcomes"
    outcomes.parent.mkdir(parents=True, exist_ok=True)
    outcomes.write_text("not a directory")
    with pytest.raises(FactoryError, match="must be a directory"):
        model_calibration(root)
    with pytest.raises(FactoryError, match="must be a directory"):
        record_model_outcome(root, value)


def test_outcome_attempt_and_plan_sources_are_bounded(root):
    record = outcome_template()
    record.update(
        kind="outcome",
        recorded_at=now(),
        content_hash="a" * 64,
        result={
            "path": ".factory/missions/M-EXAMPLE/results/records/T-001.json",
            "sha256": "a" * 64,
            "fingerprint": "b" * 64,
            "status": "blocked",
            "attempt": 0,
            "observed_model": None,
            "observed_effort": None,
            "identity_provenance": "unknown",
            "identity_reference": None,
            "profile": None,
            "harness": None,
            "catalog_client_version": None,
            "billing_context": None,
        },
    )
    with pytest.raises(FactoryError, match="Invalid model-outcome"):
        validate_outcome(root, record)
    request, catalog = inputs(root)
    plan = models.create_model_plan(root, request, catalog)
    plan["assignments"][0]["sources"] = ["https://" + "a" * 2050]
    with pytest.raises(FactoryError, match="assignments/0/sources/0"):
        models.validate_document(root, "plan", plan)
