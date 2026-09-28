"""No live inference: configured selector branches, constraints and offline receipts."""

import copy
import json
import sys

import pytest
from test_models import inputs, model

from software_factory import models, routing
from software_factory.core import FactoryError, canonical, digest, git, read_json, write_json
from software_factory.jev import JevError


@pytest.fixture
def root(tmp_path):
    git(tmp_path, "init", "-q")
    (tmp_path / ".gitignore").write_text(".factory/local/\n")
    config = {
        "schema_version": 1,
        "name": "routing-fixture",
        "profile": "codex",
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
        "jev": {"enabled": True, "provider": "typesafe", "model": "jev-1.13.0"},
    }
    write_json(tmp_path, "factory.json", config)
    return tmp_path


def setting(root, **updates):
    config = read_json(root, "factory.json")
    config["jev"].update(updates)
    write_json(root, "factory.json", config)


def response_for(body, selected="gpt-6-luna", confidence=0.9):
    choices = body["questions"]["selection"]["criteria"]
    key = (
        "abstain"
        if selected is None
        else next(c["choice"] for c in body["state"]["candidates"] if c["id"] == selected)
    )
    return {
        "model": body["model"],
        "answers": {
            "selection": {
                "type": "choice",
                "choice": key,
                "confidence": confidence,
                "probabilities": {name: 1 if name == key else 0 for name in choices},
            }
        },
        "usage": {"input_tokens": 100, "output_tokens": 1},
    }


def plan(root, request=None, catalog=None, **kwargs):
    default_request, default_catalog = inputs(root)
    return routing.route_model_plan(
        root,
        request or default_request,
        catalog or default_catalog,
        get_api_key=kwargs.pop("get_api_key", lambda: "synthetic-key"),
        transport=kwargs.pop("transport", lambda body, **_: response_for(body)),
        **kwargs,
    )


def test_off_and_missing_use_factory_models_without_key_or_network(root, monkeypatch):
    fail = lambda *_a, **_k: pytest.fail("OFF accessed provider or credentials")
    monkeypatch.setattr(routing, "request_jev", fail)
    for mode in ("disabled", "missing"):
        config = read_json(root, "factory.json")
        if mode == "disabled":
            config["jev"]["enabled"] = False
        else:
            config.pop("jev")
        write_json(root, "factory.json", config)
        result = plan(root, transport=fail, get_api_key=fail)
        assert result["routing"]["engine"] == "factory-models"
        assert result["assignments"][0]["model"]["id"] == "gpt-6-sol"
        models.validate_plan(root, result)


def test_on_jev_can_choose_reviewed_model_outside_factory_preference_order(root):
    calls = []

    def choose(body, **kwargs):
        calls.append(body)
        return response_for(body)

    result = plan(root, transport=choose)
    assert len(calls) == 1
    assert result["routing"]["engine"] == "jev"
    assert result["assignments"][0]["model"]["id"] == "gpt-6-luna"
    assert result["routing"]["decisions"][0]["reason"] == "jev_choice"
    assert result["routing"]["native_model_applied"] is False
    assert "JEV" in result["assignments"][0]["rationale"]
    assert "TYPESAFE_API_KEY" not in json.dumps(result)


def test_offline_validation_dispatch_and_historical_receipt_never_call_provider(root, monkeypatch):
    result = plan(root)
    monkeypatch.setattr(routing, "request_jev", lambda *_a, **_k: pytest.fail("Offline operation called JEV"))
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    models.validate_plan(root, result)
    dispatched = models.dispatch_assignment(root, result, "implementer", result["catalog"])
    assert dispatched["settings"]["model"] == "gpt-6-luna" and not dispatched["applied"]
    setting(root, enabled=False)
    models.validate_plan(root, result, current=False)
    with pytest.raises(FactoryError, match="changed"):
        models.validate_plan(root, result)
    with pytest.raises(FactoryError, match="changed"):
        models.dispatch_assignment(root, result, "implementer", result["catalog"])


def test_all_plans_require_engine_record_and_plain_builder_stays_offline(root):
    request, catalog = inputs(root)
    result = models.create_model_plan(root, request, catalog)
    assert result["routing"]["engine"] == "factory-models"
    with pytest.raises(FactoryError, match="engine|toggle"):
        models.validate_plan(root, result)
    result.pop("routing")
    with pytest.raises(FactoryError, match="routing"):
        models.validate_plan(root, result)


def many_candidates(root, count):
    request, catalog = inputs(root)
    identifiers = [f"gpt-6-sol-variant-{i:03d}" for i in range(count)]
    catalog["models"] = [model(identifier) for identifier in identifiers]
    request["research"] = [
        {
            "key": "openai-sol-variants",
            "provider": "openai",
            "profiles": ["codex"],
            "model_ids": identifiers,
            "strengths": "Demanding reasoning and long-horizon agentic work; consider when the task warrants escalation.",
            "sources": ["https://developers.openai.com/api/docs/models/gpt-6-sol"],
            "checked_at": catalog["observed_at"],
        }
    ]
    return request, catalog


def test_fifty_compact_candidates_fit_default_request_limit_and_replay(root):
    request, catalog = many_candidates(root, 50)
    sizes = []

    def choose(body, **_):
        sizes.append(len(canonical(body)))
        for candidate in body["state"]["candidates"]:
            assert set(candidate) <= {"choice", "strengths", *routing.CANDIDATE_FIELDS}
            assert None not in candidate.values()
            assert candidate["strengths"].startswith("Demanding reasoning")
        return response_for(body, "gpt-6-sol-variant-049")

    result = plan(root, request, catalog, transport=choose)
    assert len(sizes) == 1 and sizes[0] <= routing.DEFAULTS["max_request_bytes"]
    assert result["assignments"][0]["model"]["id"] == "gpt-6-sol-variant-049"
    assert len(result["assignments"][0]["fallbacks"]) == 49
    body = json.dumps(result)
    assert '"sources"' in body  # Plan keeps full guidance; only the JEV request is compact.
    models.validate_plan(root, result)
    tampered = copy.deepcopy(result)
    tampered["routing"]["decisions"][0]["request_hash"] = digest({"different": "request"})
    tampered["routing"]["record_hash"] = digest(
        {k: v for k, v in tampered["routing"].items() if k != "record_hash"}
    )
    with pytest.raises(FactoryError, match="request changed"):
        models.validate_plan(root, tampered)


def test_candidate_ceiling_is_documented_and_enforced_before_credentials(root):
    request, catalog = inputs(root)
    item = request["assignments"][0]

    def size(count):
        eligible = [
            {
                "model": model(f"gpt-6-sol-variant-{i:03d}"),
                "guidance": {"strengths": "x" * 94, "sources": ["https://example.com"]},
                "preferred": False,
            }
            for i in range(count)
        ]
        body, _, _ = routing._decision_body(root, request, item, item["requirements"], eligible, "jev-1.13.0")
        return len(canonical(body))

    ceiling = max(n for n in range(1, routing.MAX_CANDIDATES + 1) if size(n) <= 24576)
    assert 50 <= ceiling < routing.MAX_CANDIDATES
    request, catalog = many_candidates(root, ceiling + 1)
    fail = lambda *_a, **_k: pytest.fail("Oversize candidate set reached credentials or provider")
    with pytest.raises(FactoryError, match="byte limit; no fallback") as raised:
        plan(root, request, catalog, get_api_key=fail, transport=fail)
    assert raised.value.exit_code == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("effort", "extreme"),
        ("min_context_tokens", 100001),
        ("allowed_providers", ["other"]),
        ("exact_model", "absent"),
        ("max_cost_tier", 0),
    ],
)
def test_hard_constraints_reject_before_jev(root, field, value):
    request, catalog = inputs(root)
    request["assignments"][0]["requirements"][field] = value
    result = plan(
        root,
        request,
        catalog,
        transport=lambda *_a, **_k: pytest.fail("No eligible candidate must not call JEV"),
    )
    assert result["assignments"][0]["status"] == "unresolved"
    assert result["routing"]["decisions"][0]["reason"] == "no_eligible_candidates"
    models.validate_plan(root, result)


def test_jev_receives_only_hard_eligible_reviewed_candidates(root):
    request, catalog = inputs(root)
    catalog["models"] += [model("unreviewed"), model("unavailable", availability="unavailable")]
    request["assignments"][0]["requirements"].update(max_cost_tier=1, effort="high", exact_model="gpt-6-luna")

    def choose(body, **_):
        assert [c["id"] for c in body["state"]["candidates"]] == ["gpt-6-luna"]
        return response_for(body)

    result = plan(root, request, catalog, transport=choose)
    assert result["assignments"][0]["effort"] == "high"
    assert {r["model"] for r in result["assignments"][0]["rejected"]} == {
        "gpt-6-astra",
        "gpt-6-sol",
        "unreviewed",
        "unavailable",
    }


@pytest.mark.parametrize("patch", [{"session_id": "OTHER"}, {"observed_at": "2020-01-01T00:00:00Z"}])
def test_catalog_session_and_freshness_remain_hard_constraints(root, patch):
    # Intentional change (M-13): planning is refused before any selector runs.
    request, catalog = inputs(root)
    catalog.update(patch)
    fail = lambda *_a, **_k: pytest.fail("Stale inventory routed")
    with pytest.raises(FactoryError, match="Cannot plan with this catalog"):
        plan(root, request, catalog, transport=fail, get_api_key=fail)


def test_explicit_preference_cannot_be_changed_or_silently_substituted(root):
    request, catalog = inputs(root)
    request["assignments"][0].update(
        preferred_model="gpt-6-sol", rationale="The user selected this exact eligible model"
    )
    fail = lambda *_a, **_k: pytest.fail("Explicit choice should not incur a JEV request")
    result = plan(root, request, catalog, transport=fail, get_api_key=fail)
    assert result["assignments"][0]["model"]["id"] == "gpt-6-sol"
    assert result["routing"]["decisions"][0]["reason"] == "explicit_preference"
    models.validate_plan(root, result)
    request["assignments"][0]["preferred_model"] = "absent"
    result = plan(root, request, catalog, transport=fail)
    assert result["assignments"][0]["status"] == "unresolved"
    assert result["routing"]["decisions"][0]["reason"] == "explicit_preference_unavailable"


def test_copilot_child_filters_use_actual_selected_parent_tier(root):
    request, catalog = inputs(root, "copilot", "copilot-local")
    child = request["assignments"][0]
    main = copy.deepcopy(child)
    main.update(id="orchestrator", role="orchestrator", intent="complex")
    main["requirements"]["operation"] = "main"
    request["assignments"] = [child, main]  # Builder must resolve main first.
    calls = []

    def choose(body, **_):
        calls.append(body)
        if body["state"]["assignment"]["requirements"]["operation"] == "subagent":
            assert body["state"]["assignment"]["requirements"]["parent_cost_tier"] == 1
            assert [c["id"] for c in body["state"]["candidates"]] == ["gpt-6-luna"]
        return response_for(body)

    result = plan(root, request, catalog, transport=choose)
    # The child has one hard-eligible candidate, so only the parent is sent to JEV.
    assert [b["state"]["assignment"]["id"] for b in calls] == ["orchestrator"]
    reasons = {r["assignment_id"]: r["reason"] for r in result["routing"]["decisions"]}
    assert reasons == {"orchestrator": "jev_choice", "implementer": "single_eligible_candidate"}
    models.validate_plan(root, result)
    catalog["parent_model"] = "gpt-6-luna"
    assert (
        models.dispatch_assignment(root, result, "implementer", catalog)["settings"]["model"] == "gpt-6-luna"
    )
    catalog["parent_model"] = "gpt-6-sol"
    with pytest.raises(FactoryError, match="parent tier"):
        models.dispatch_assignment(root, result, "implementer", catalog)


def test_abstention_returns_unresolved_without_factory_fallback(root):
    result = plan(root, transport=lambda body, **_: response_for(body, None))
    assert result["assignments"][0]["status"] == "unresolved"
    assert result["routing"]["decisions"][0]["reason"] == "abstained"
    models.validate_plan(root, result)
    with pytest.raises(FactoryError, match="unresolved"):
        models.dispatch_assignment(root, result, "implementer", result["catalog"])


@pytest.mark.parametrize("failure", ["missing_key", "timeout", "malformed", "exception"])
def test_on_failures_are_explicit_and_never_fallback(root, failure):
    def provider(body, **_):
        if failure == "timeout":
            raise JevError("deadline_exceeded")
        if failure == "exception":
            raise RuntimeError("SECRET upstream body")
        return {"private": "SECRET invalid response"}

    with pytest.raises(FactoryError) as raised:
        plan(
            root,
            get_api_key=lambda: None if failure == "missing_key" else "synthetic-key",
            transport=provider,
        )
    assert raised.value.exit_code == 2
    assert "SECRET" not in str(raised.value)


def test_request_and_plan_bounds_precede_inference(root):
    setting(root, max_request_bytes=1024)
    with pytest.raises(FactoryError, match="byte limit"):
        plan(root, get_api_key=lambda: pytest.fail("Oversize read key"))
    setting(root, max_request_bytes=24576)
    request, catalog = inputs(root)
    request["assignments"] = [
        {**copy.deepcopy(request["assignments"][0]), "id": f"task{i}"} for i in range(17)
    ]
    with pytest.raises(FactoryError, match="16 assignments"):
        plan(root, request, catalog, get_api_key=lambda: pytest.fail("Assignment limit read key"))


class FakeClock:
    """Deterministic monotonic clock advanced only by mocked provider latency."""

    def __init__(self):
        self.value = 1000.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def assignments(request, count):
    base = request["assignments"][0]
    request["assignments"] = [{**copy.deepcopy(base), "id": f"role{i}"} for i in range(count)]
    return request


def test_each_request_gets_full_deadline_under_realistic_latency(root, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(routing, "time", clock)
    request, catalog = inputs(root)
    assignments(request, 5)
    calls = []

    def latent(body, **kwargs):
        calls.append(kwargs["deadline_ms"])
        clock.sleep(0.8)
        return response_for(body)

    result = plan(root, request, catalog, transport=latent)
    assert calls == [3000] * 5
    assert result["routing"]["provider_requests"] == 5
    assert all(item["status"] == "selected" for item in result["assignments"])
    models.validate_plan(root, result)


def test_late_successful_response_is_kept_not_discarded(root, monkeypatch):
    # Intentional change (item 10): a response that arrives at the end of its
    # budget is already billed and is kept; the transport enforces its deadline.
    clock = FakeClock()
    monkeypatch.setattr(routing, "time", clock)
    setting(root, deadline_ms=1000)
    request, catalog = inputs(root)
    assignments(request, 2)
    calls = []

    def slow(body, **kwargs):
        calls.append(kwargs["deadline_ms"])
        clock.sleep(1.0)
        return response_for(body)

    result = plan(root, request, catalog, transport=slow)
    assert calls == [1000, 1000]
    assert result["routing"]["provider_requests"] == 2


def test_overall_plan_budget_caps_sequential_requests(root, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(routing, "time", clock)
    monkeypatch.setattr(routing, "MAX_PLAN_DEADLINE_MS", 1500)
    setting(root, deadline_ms=1000)
    request, catalog = inputs(root)
    assignments(request, 3)
    calls = []

    def slow(body, **kwargs):
        calls.append(kwargs["deadline_ms"])
        clock.sleep(0.9)
        return response_for(body)

    with pytest.raises(FactoryError, match="deadline exceeded; no fallback") as raised:
        plan(root, request, catalog, transport=slow)
    assert raised.value.exit_code == 2
    assert calls == [1000, 600]


def test_plan_budget_is_per_assignment_and_bounded():
    assert routing.MAX_PLAN_DEADLINE_MS == 120000
    assert routing.MAX_PLAN_DEADLINE_MS >= routing.DEFAULTS["deadline_ms"] * routing.MAX_ASSIGNMENTS


@pytest.mark.parametrize("mutation", ["toggle", "transient_toggle", "rubric", "input", "schema"])
def test_changes_during_selection_prevent_publication(root, mutation):
    request, catalog = inputs(root)

    def change(body, **_):
        if mutation == "toggle":
            setting(root, enabled=False)
        elif mutation == "transient_toggle":
            setting(root, enabled=False)
            setting(root, enabled=True)
        elif mutation == "input":
            request["objective"] += " changed"
        else:
            relative = (
                "models/selection-rubric.json" if mutation == "rubric" else "schemas/models.schema.json"
            )
            source = routing.asset_path(root, relative)
            destination = root / ".factory" / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(source.read_bytes() + b"\n")
        return response_for(body)

    with pytest.raises(FactoryError, match="changed"):
        plan(root, request, catalog, transport=change)


def test_input_file_changes_and_current_control_changes_are_detected(root):
    request, catalog = inputs(root)
    write_json(root, ".factory/local/request.json", request)
    write_json(root, ".factory/local/catalog.json", catalog)

    def rewrite(body, **_):
        write_json(root, ".factory/local/request.json", {**request, "objective": "Changed after dispatch"})
        return response_for(body)

    with pytest.raises(FactoryError, match="changed"):
        plan(
            root,
            request,
            catalog,
            transport=rewrite,
            input_paths={"request": ".factory/local/request.json", "catalog": ".factory/local/catalog.json"},
        )
    result = plan(root)
    setting(root, max_response_bytes=32768)
    with pytest.raises(FactoryError, match="configuration"):
        models.validate_plan(root, result)
    models.validate_plan(root, result, current=False)


@pytest.mark.parametrize(
    "edit",
    [
        lambda c: c["jev"].update(claim_mode="advisory"),
        lambda c: c["jev"].update(claim_accept_confidence=0.95),
        lambda c: c["jev"].update(max_pairs=8, cache=True, cache_ttl_seconds=60, max_source_age_hours=24),
        lambda c: c["limits"].update(repair_attempts=5),
        lambda c: c["checks"][0].update(timeout_seconds=31),
        lambda c: c.update(name="renamed-fixture"),
    ],
)
def test_routing_unrelated_configuration_edits_keep_plan_valid(root, edit):
    result = plan(root)
    config = read_json(root, "factory.json")
    edit(config)
    write_json(root, "factory.json", config)
    models.validate_plan(root, result)
    assert models.dispatch_assignment(root, result, "implementer", result["catalog"])["settings"]["model"]


@pytest.mark.parametrize(
    "edit",
    [
        lambda c: c["jev"].update(enabled=False),
        lambda c: c["jev"].update(deadline_ms=5000),
        lambda c: c["jev"].update(max_request_bytes=16384),
        lambda c: c["jev"].update(model="jev-1.14.0"),
        lambda c: c["jev"].update(min_confidence=0.7),
        lambda c: c.update(profile=["codex", "claude"]),
        lambda c: c.update(model_selection={"mode": "required"}),
    ],
)
def test_routing_relevant_configuration_edits_invalidate_plan(root, edit):
    result = plan(root)
    config = read_json(root, "factory.json")
    edit(config)
    write_json(root, "factory.json", config)
    with pytest.raises(FactoryError, match="changed"):
        models.validate_plan(root, result)
    models.validate_plan(root, result, current=False)


def test_unsupported_provider_change_fails_exactly(root):
    result = plan(root)
    config = read_json(root, "factory.json")
    config["jev"]["provider"] = "other"
    (root / "factory.json").write_text(json.dumps(config))
    with pytest.raises(FactoryError, match="Invalid factory at jev"):
        models.validate_plan(root, result)


@pytest.mark.parametrize("tamper", ["receipt", "selection", "engine", "remove"])
def test_replay_detects_tampering_without_provider(root, tamper):
    result = plan(root)
    if tamper == "receipt":
        result["routing"]["decisions"][0]["response"]["usage"]["input_tokens"] += 1
    elif tamper == "selection":
        result["assignments"][0]["model"] = model("gpt-6-sol")
    elif tamper == "engine":
        result["routing"]["engine"] = "factory-models"
        result["routing"]["record_hash"] = digest(
            {k: v for k, v in result["routing"].items() if k != "record_hash"}
        )
    else:
        result.pop("routing")
    with pytest.raises(FactoryError):
        models.validate_plan(root, result)


def test_minimum_native_client_version_cannot_be_overridden_by_jev(root):
    request, catalog = inputs(root, "claude", "claude-code-native")
    catalog["client_version"] = "0.0.1"
    catalog["models"] = [model("claude-opus-5-5", provider="anthropic")]
    request["assignments"][0].update(
        preferred_model="claude-opus-5-5", rationale="Preference still requires a supported client"
    )
    result = plan(
        root, request, catalog, transport=lambda *_a, **_k: pytest.fail("Unsupported client called JEV")
    )
    assert result["assignments"][0]["status"] == "unresolved"
    assert any("Client version" in reason for reason in result["assignments"][0]["rejected"][0]["reasons"])
    assert result["routing"]["provider_requests"] == 0


def test_provider_call_count_matches_saved_receipts(root):
    result = plan(root)
    assert result["routing"]["provider_requests"] == 1
    result["routing"]["provider_requests"] = 0
    result["routing"]["record_hash"] = digest(
        {k: v for k, v in result["routing"].items() if k != "record_hash"}
    )
    with pytest.raises(FactoryError, match="request count"):
        models.validate_plan(root, result, current=False)


def test_reasoned_explicit_new_model_is_authoritative_without_jev_guidance(root):
    request, catalog = inputs(root)
    catalog["models"].append(model("user-exact-model"))
    request["assignments"][0].update(
        preferred_model="user-exact-model",
        rationale="User supplied this hard-eligible exact model after task-specific investigation",
    )
    fail = lambda *_a, **_k: pytest.fail("Explicit selection accessed JEV or credentials")
    result = plan(root, request, catalog, get_api_key=fail, transport=fail)
    assert result["assignments"][0]["model"]["id"] == "user-exact-model"
    assert result["routing"]["provider_requests"] == 0
    assert result["routing"]["decisions"][0]["reason"] == "explicit_preference"
    models.validate_plan(root, result)
    request["assignments"][0].update(preferred_model=None, rationale=None)

    def choose(body, **_):
        assert "user-exact-model" not in {c["id"] for c in body["state"]["candidates"]}
        return response_for(body)

    result = plan(root, request, catalog, transport=choose)
    assert result["assignments"][0]["model"]["id"] == "gpt-6-luna"


def run_plan_cli(root, output):
    import argparse

    parser = argparse.ArgumentParser()
    models.add_parser(parser.add_subparsers())
    args = parser.parse_args(
        ["models", "plan", "--input", "request.json", "--catalog", "catalog.json", "--output", output]
    )
    args.root = root
    return args.handler(args)


def snapshot_tree(root):
    return {
        path.relative_to(root).as_posix(): (
            ("symlink", str(path.readlink()))
            if path.is_symlink()
            else ("directory", None)
            if path.is_dir()
            else ("file", path.read_bytes())
        )
        for path in root.rglob("*")
        if ".git" not in path.relative_to(root).parts
    }


@pytest.mark.parametrize(
    "invalid",
    ["outside", "unignored", "tracked", "output_directory", "output_symlink", "local_file", "models_file"],
)
def test_plan_cli_invalid_output_precedes_credentials_and_provider(root, monkeypatch, invalid):
    request, catalog = inputs(root)
    write_json(root, "request.json", request)
    write_json(root, "catalog.json", catalog)
    output = ".factory/local/models/plan.json"
    target = root / output
    if invalid == "outside":
        output = "plan.json"
    elif invalid == "unignored":
        (root / ".gitignore").write_text("")
    elif invalid == "tracked":
        write_json(root, output, {"previous": "preserved"})
        git(root, "add", "--force", "--", output)
    elif invalid == "output_directory":
        target.mkdir(parents=True)
    elif invalid == "output_symlink":
        target.parent.mkdir(parents=True)
        target.symlink_to(root / "request.json")
    elif invalid == "local_file":
        (root / ".factory").mkdir()
        (root / ".factory/local").write_text("existing file")
    elif invalid == "models_file":
        target.parent.parent.mkdir(parents=True)
        target.parent.write_text("existing file")
    before = snapshot_tree(root)
    calls = []
    original = routing.route_model_plan

    def forbidden(*_args, **_kwargs):
        calls.append("accessed")
        pytest.fail("Invalid output reached credentials or provider")

    monkeypatch.setattr(
        routing,
        "route_model_plan",
        lambda *args, **kwargs: original(*args, get_api_key=forbidden, transport=forbidden, **kwargs),
    )
    with pytest.raises(FactoryError, match="output|Private|ignore|Symlink"):
        run_plan_cli(root, output)
    assert calls == []
    assert snapshot_tree(root) == before


@pytest.mark.parametrize("succeed", [False, True])
def test_plan_cli_creates_output_only_after_successful_planning(root, monkeypatch, succeed):
    request, catalog = inputs(root)
    write_json(root, "request.json", request)
    write_json(root, "catalog.json", catalog)
    output = ".factory/local/models/plan.json"
    original = routing.route_model_plan
    calls = []

    def choose(body, **_kwargs):
        calls.append("provider")
        assert not (root / ".factory").exists()
        if not succeed:
            raise JevError("deadline_exceeded")
        return response_for(body)

    monkeypatch.setattr(
        routing,
        "route_model_plan",
        lambda *args, **kwargs: original(
            *args, get_api_key=lambda: "synthetic-key", transport=choose, **kwargs
        ),
    )
    if succeed:
        assert run_plan_cli(root, output)["written"] == output
        models.validate_plan(root, read_json(root, output))
    else:
        with pytest.raises(FactoryError, match="JEV model routing deadline_exceeded; no fallback") as raised:
            run_plan_cli(root, output)
        assert raised.value.exit_code == 2
        assert not (root / ".factory").exists()
    assert calls == ["provider"]


def test_low_confidence_choice_is_unresolved_without_fallback(root, monkeypatch):
    result = plan(root, transport=lambda body, **_: response_for(body, confidence=0.55))
    item, receipt = result["assignments"][0], result["routing"]["decisions"][0]
    assert item["status"] == "unresolved" and item["model"] is None
    assert "gpt-6-sol" in item["fallbacks"] and "gpt-6-luna" in item["fallbacks"]
    assert receipt["reason"] == "low_confidence" and receipt["selected_model"] is None
    assert (receipt["confidence"], receipt["threshold"], receipt["attempts"]) == (0.55, 0.6, 1)
    assert result["routing"]["contract"]["settings"]["min_confidence"] == 0.6
    assert result["routing"]["provider_requests"] == 1
    monkeypatch.setattr(routing, "request_jev", lambda *_a, **_k: pytest.fail("Replay called JEV"))
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    models.validate_plan(root, result)
    with pytest.raises(FactoryError, match="unresolved"):
        models.dispatch_assignment(root, result, "implementer", result["catalog"])


@pytest.mark.parametrize(
    ("minimum", "confidence", "reason"),
    [
        (None, 0.7, "jev_choice"),
        (None, 0.6, "jev_choice"),
        (0.5, 0.55, "jev_choice"),
        (0.9, 0.85, "low_confidence"),
    ],
)
def test_confidence_threshold_is_configurable_and_replayed_offline(
    root, monkeypatch, minimum, confidence, reason
):
    if minimum is not None:
        setting(root, min_confidence=minimum)
    result = plan(root, transport=lambda body, **_: response_for(body, confidence=confidence))
    receipt = result["routing"]["decisions"][0]
    assert receipt["reason"] == reason
    assert receipt["threshold"] == (0.6 if minimum is None else minimum)
    assert receipt["confidence"] == confidence
    selected = result["assignments"][0]["model"]
    assert (selected["id"] if selected else None) == ("gpt-6-luna" if reason == "jev_choice" else None)
    monkeypatch.setattr(routing, "request_jev", lambda *_a, **_k: pytest.fail("Replay called JEV"))
    assert routing.replay_model_plan(root, result) == result
    models.validate_plan(root, result)


def test_abstention_precedes_confidence_gate_and_attempts_are_recorded(root):
    def retried(body, **kwargs):
        kwargs["on_attempt"](1)
        kwargs["on_attempt"](2)
        return response_for(body, None, confidence=0.2)

    result = plan(root, transport=retried)
    receipt = result["routing"]["decisions"][0]
    assert (receipt["reason"], receipt["attempts"], receipt["confidence"]) == ("abstained", 2, 0.2)
    assert result["routing"]["provider_requests"] == 1
    models.validate_plan(root, result)


def rehash(result):
    result["routing"]["record_hash"] = digest(
        {k: v for k, v in result["routing"].items() if k != "record_hash"}
    )
    return result


@pytest.mark.parametrize("tamper", ["threshold", "confidence", "reason", "missing", "explicit"])
def test_confidence_gate_receipts_cannot_be_rewritten(root, tamper):
    result = plan(root, transport=lambda body, **_: response_for(body, confidence=0.55))
    receipt = result["routing"]["decisions"][0]
    if tamper == "threshold":
        receipt["threshold"] = 0.5
    elif tamper == "confidence":
        receipt["confidence"] = 0.9
    elif tamper == "reason":
        receipt["reason"] = "abstained"
    elif tamper == "missing":
        receipt.pop("attempts")
    else:
        receipt.update(request_hash=None, response=None, reason="no_eligible_candidates")
        result["routing"]["provider_requests"] = 0
    rehash(result)
    with pytest.raises(FactoryError):
        models.validate_plan(root, result, current=False)


def test_plans_recorded_before_the_confidence_gate_still_validate(root):
    result = plan(root, transport=lambda body, **_: response_for(body, confidence=0.3))
    assert result["routing"]["decisions"][0]["reason"] == "low_confidence"
    # Simulate an older plan: no contract threshold and no receipt gate fields.
    result["routing"]["contract"]["settings"].pop("min_confidence")
    receipt = result["routing"]["decisions"][0]
    for key in ("confidence", "threshold", "attempts"):
        receipt.pop(key)
    receipt.update(reason="jev_choice", selected_model="gpt-6-luna")
    result["assignments"][0] = plan(root)["assignments"][0]
    rehash(result)
    models.validate_plan(root, result, current=False)
    receipt["reason"] = "low_confidence"
    rehash(result)
    with pytest.raises(FactoryError):
        models.validate_plan(root, result, current=False)
    # Current validation still requires regeneration under today's contract.
    receipt["reason"] = "jev_choice"
    rehash(result)
    with pytest.raises(FactoryError, match="changed"):
        models.validate_plan(root, result)


def test_plan_cli_low_confidence_writes_unresolved_plan(root, monkeypatch):
    request, catalog = inputs(root)
    write_json(root, "request.json", request)
    write_json(root, "catalog.json", catalog)
    output = ".factory/local/models/plan.json"
    original = routing.route_model_plan
    monkeypatch.setattr(
        routing,
        "route_model_plan",
        lambda *args, **kwargs: original(
            *args,
            get_api_key=lambda: "synthetic-key",
            transport=lambda body, **_: response_for(body, confidence=0.1),
            **kwargs,
        ),
    )
    assert run_plan_cli(root, output)["written"] == output
    saved = read_json(root, output)
    assert saved["assignments"][0]["status"] == "unresolved"
    assert saved["routing"]["decisions"][0]["reason"] == "low_confidence"
    models.validate_plan(root, saved)


def test_plan_uses_saved_credential_and_fails_closed_on_invalid_store(root):
    from software_factory import auth

    key = "synthetic-routing-stored-key"
    auth.save_typesafe_key(key)
    calls = []

    def choose(body, **options):
        assert options["api_key"] == key
        calls.append(body)
        return response_for(body)

    result = plan(root, get_api_key=None, transport=choose)
    assert calls and result["routing"]["engine"] == "jev"
    assert key not in json.dumps(result)
    (auth._directory() / auth.FILENAME).write_text("broken-store")
    with pytest.raises(FactoryError, match="credential_unavailable.*auth status"):
        plan(root, get_api_key=None, transport=lambda *_a, **_k: pytest.fail("bad credential used"))


# 0.3.2 hardening (WP5): masking, single candidates and attempt counts.


def test_objective_and_strengths_are_masked_and_counted(root, monkeypatch):
    request, catalog = inputs(root)
    request["objective"] = "Rotate password=hunter2secret before release"
    request["research"] = [
        {
            "key": "research-luna",
            "provider": "openai",
            "profiles": ["codex"],
            "model_ids": ["gpt-6-luna-research"],
            "strengths": "Fast. token ghp_abcdefghijklmnopqrstuvwxyz0123456789",
            "sources": ["https://example.com/luna"],
            "checked_at": catalog["observed_at"],
        }
    ]
    catalog["models"].append(model("gpt-6-luna-research"))
    sent = []

    def choose(body, **_):
        sent.append(json.dumps(body))
        return response_for(body)

    result = plan(root, request, catalog, transport=choose)
    assert "hunter2" not in sent[0] and "ghp_" not in sent[0]
    assert "[REDACTED:" in sent[0]
    receipt = result["routing"]["decisions"][0]
    assert receipt["masks"] == 2
    monkeypatch.setattr(routing, "request_jev", lambda *_a, **_k: pytest.fail("Replay called JEV"))
    models.validate_plan(root, result)
    receipt["masks"] = 0
    rehash(result)
    with pytest.raises(FactoryError, match="request changed"):
        models.validate_plan(root, result)


def test_single_eligible_candidate_skips_the_paid_request(root):
    request, catalog = inputs(root)
    catalog["models"] = [model("gpt-6-luna", cost_tier=1)]
    fail = lambda *_a, **_k: pytest.fail("A single candidate reached credentials or JEV")
    result = plan(root, request, catalog, transport=fail, get_api_key=fail)
    item, receipt = result["assignments"][0], result["routing"]["decisions"][0]
    assert item["model"]["id"] == "gpt-6-luna"
    assert receipt["reason"] == "single_eligible_candidate" and receipt["response"] is None
    assert "masks" not in receipt and result["routing"]["provider_requests"] == 0
    assert "no JEV request" in item["rationale"]
    models.validate_plan(root, result)
    receipt["masks"] = 0
    rehash(result)
    with pytest.raises(FactoryError, match="cannot invent a JEV request"):
        models.validate_plan(root, result, current=False)


@pytest.mark.parametrize("reported", [[1, 1], [2], [1, 2, 3, 4]])
def test_inconsistent_transport_attempt_counts_fail_closed(root, reported):
    def transport(body, **kwargs):
        for number in reported:
            kwargs["on_attempt"](number)
        return response_for(body)

    with pytest.raises(FactoryError, match="inconsistent attempts") as raised:
        plan(root, transport=transport)
    assert raised.value.exit_code == 2


def test_context_takes_one_stat_per_watched_path(root, monkeypatch):
    calls = []
    original = routing.Path.stat

    def counting(self, *args, **kwargs):
        calls.append(self)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(routing.Path, "stat", counting)
    request, catalog = inputs(root)
    _, stamps = routing._context(root, request, catalog)
    assert len(calls) == len(stamps) == len(set(calls))
