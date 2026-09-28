"""Model planning, metadata protocol and evidence guardrails."""

import argparse
import copy
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
    "patch",
    [
        {"observed_at": "2020-01-01T00:00:00Z"},
        {"observed_at": "2099-01-01T00:00:00Z"},
        {"session_id": "DIFFERENT"},
    ],
)
def test_freshness_and_session(root, patch):
    request, catalog = inputs(root)
    catalog.update(patch)
    assert models.create_model_plan(root, request, catalog)["assignments"][0]["status"] == "unresolved"


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
    request["research"] = [custom]
    catalog["client_version"] = "0.0.1"
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
    assert (
        models.dispatch_assignment(root, plan, "implementer", {**catalog, "session_id": "NEW"})["settings"][
            "model"
        ]
        == "gpt-6-sol"
    )
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
    catalog = models.discover_codex(root, binary, "TEST", provider="openai", timeout_ms=1000)
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
    with pytest.raises(FileExistsError):
        record_model_outcome(root, value)
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
