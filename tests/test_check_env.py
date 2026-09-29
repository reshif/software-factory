"""0.3.2 F-3: check environment allowlist and reviewers without web access."""

import json
import os
import sys

import pytest
import yaml
from test_workflow import begin, commit, make_repo

from software_factory.checks import (
    AUTH_DISABLED,
    RUN_MARKER,
    check_environment,
    environment_reasons,
    run_check,
    run_checks,
    validate_verification,
    verify_mission,
)
from software_factory.core import FactoryError, load_config, read_json, validate
from software_factory.evidence import fingerprint
from software_factory.installation import install
from software_factory.rendering import render
from software_factory.workflow import load_mission

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX PATH and executable bits")

# Prints the names of the environment the check received.
DUMP = "import json, os; print(json.dumps(sorted(os.environ)))"


def received(result):
    return set(json.loads(result["stdout"]))


def set_factory(root, **changes):
    config = json.loads((root / "factory.json").read_text())
    for key, value in changes.items():
        if key == "check":
            config["checks"][0].update(value)
        else:
            config[key] = value
    (root / "factory.json").write_text(json.dumps(config, indent=2) + "\n")


@pytest.fixture
def secrets(monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-secret")
    monkeypatch.setenv("F3_DEPLOY_TOKEN", "deploy-secret")
    monkeypatch.setenv("F3_NEEDED", "needed")
    monkeypatch.setenv("PYTHONF3", "python-setting")
    monkeypatch.setenv("LC_F3", "C")
    monkeypatch.setenv("HOME", os.environ.get("HOME", "/tmp"))


# The environment a check receives.


def test_inherit_mode_keeps_todays_behaviour_without_the_inference_key(secrets):
    env = check_environment({"id": "unit"})
    assert "TYPESAFE_API_KEY" not in env
    assert env["F3_DEPLOY_TOKEN"] == "deploy-secret" and env["PYTHONF3"] == "python-setting"
    assert env[AUTH_DISABLED] == "1"


def test_allowlist_passes_only_the_safe_base_and_listed_names(secrets, monkeypatch):
    monkeypatch.delenv("F3_ABSENT", raising=False)
    env = check_environment({"id": "unit", "env": ["F3_NEEDED", "F3_ABSENT"]}, "allowlist")
    assert env["F3_NEEDED"] == "needed" and "F3_ABSENT" not in env
    assert env["PATH"] == os.environ["PATH"] and env["HOME"] and env["LC_F3"] == "C"
    for hidden in ("F3_DEPLOY_TOKEN", "PYTHONF3", "TYPESAFE_API_KEY"):
        assert hidden not in env
    assert env[AUTH_DISABLED] == "1"
    # PYTHON* passes only when listed.
    assert check_environment({"id": "unit", "env": ["PYTHONF3"]}, "allowlist")["PYTHONF3"] == "python-setting"
    # The global allowlist mode with no per-check names gives only the base.
    base = check_environment({"id": "unit"}, "allowlist")
    assert "F3_NEEDED" not in base and "PATH" in base


@pytest.mark.parametrize("name", ["TYPESAFE_API_KEY", "typesafe_api_key"])
def test_the_inference_key_can_never_be_listed(secrets, name):
    with pytest.raises(FactoryError, match="may not receive TYPESAFE_API_KEY"):
        check_environment({"id": "unit", "env": [name]}, "allowlist")
    with pytest.raises(FactoryError, match="may not receive TYPESAFE_API_KEY"):
        run_check(".", {"id": "unit", "command": [sys.executable, "-c", "pass"], "env": [name]})


@pytest.mark.parametrize("name", ["1BAD", "BAD-NAME", "A=B", "", 5])
def test_malformed_names_are_refused(name):
    with pytest.raises(FactoryError, match="Invalid environment variable name"):
        check_environment({"id": "unit", "env": [name]}, "allowlist")


def test_unknown_mode_is_refused():
    with pytest.raises(FactoryError, match="Invalid environment configuration"):
        check_environment({"id": "unit"}, "sandbox")


def test_check_process_receives_only_the_allowlist_and_records_names(tmp_path, secrets):
    result = run_check(
        tmp_path, {"id": "dump", "command": [sys.executable, "-c", DUMP], "env": ["F3_NEEDED"]}
    )
    assert result["status"] == "pass"
    names = received(result)
    assert "F3_NEEDED" in names and "PATH" in names and RUN_MARKER in names and AUTH_DISABLED in names
    assert not names & {"F3_DEPLOY_TOKEN", "PYTHONF3", "TYPESAFE_API_KEY"}
    assert result["environment"]["mode"] == "allowlist"
    # The record holds names, never values, and matches what the process saw
    # (a platform may add a variable such as __CF_USER_TEXT_ENCODING on macOS).
    assert set(result["environment"]["names"]) <= names
    assert "needed" not in json.dumps(result["environment"])


def test_inherit_mode_records_names_without_the_inference_key(tmp_path, secrets):
    result = run_check(tmp_path, {"id": "dump", "command": [sys.executable, "-c", DUMP]})
    assert result["environment"]["mode"] == "inherit"
    assert {"F3_DEPLOY_TOKEN", "PYTHONF3"} <= set(result["environment"]["names"])
    assert "TYPESAFE_API_KEY" not in received(result)
    assert "TYPESAFE_API_KEY" not in result["environment"]["names"]
    assert "deploy-secret" not in json.dumps(result["environment"])


def test_allowlist_run_check_mode_argument(tmp_path, secrets):
    result = run_check(
        tmp_path, {"id": "dump", "command": [sys.executable, "-c", DUMP]}, env_mode="allowlist"
    )
    assert result["environment"]["mode"] == "allowlist"
    assert not received(result) & {"F3_NEEDED", "F3_DEPLOY_TOKEN", "TYPESAFE_API_KEY"}


@posix_only
def test_program_resolution_still_uses_path_in_allowlist_mode(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    tool = tools / "f3-tool"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
    result = run_check(tmp_path, {"id": "tool", "command": ["f3-tool"], "env": []})
    assert result["status"] == "pass" and result["resolved_program"] == str(tool)
    assert result["environment"]["mode"] == "allowlist"


# factory.json configuration.


def test_config_accepts_env_lists_and_check_env_mode(tmp_path):
    root = make_repo(tmp_path / "product")
    set_factory(root, check={"env": ["F3_NEEDED", "_X1"]}, check_env={"mode": "allowlist"})
    config = load_config(root)
    assert config["checks"][0]["env"] == ["F3_NEEDED", "_X1"]
    set_factory(
        root, setup=[{"id": "prep", "command": ["true"], "cwd": ".", "timeout_seconds": 5, "env": ["A"]}]
    )
    load_config(root)


@pytest.mark.parametrize(
    "changes",
    [
        {"check": {"env": ["TYPESAFE_API_KEY"]}},
        {"check": {"env": ["Typesafe_Api_Key"]}},
        {"check": {"env": ["1BAD"]}},
        {"check": {"env": ["A", "A"]}},
        {"check": {"env": "PATH"}},
        {"check_env": {"mode": "sandbox"}},
        {"check_env": {}},
        {"check_env": {"mode": "inherit", "env": ["A"]}},
    ],
)
def test_config_refuses_invalid_environment_settings(tmp_path, changes):
    root = make_repo(tmp_path / "product")
    set_factory(root, **changes)
    with pytest.raises(FactoryError, match="Invalid factory"):
        load_config(root)


# Suites, evidence and the gate.


def test_check_env_allowlist_applies_to_every_check_and_setup(tmp_path, secrets):
    root = make_repo(
        tmp_path / "product",
        DUMP,
        setup=[{"id": "prep", "command": [sys.executable, "-c", "pass"], "cwd": ".", "timeout_seconds": 5}],
    )
    set_factory(root, check_env={"mode": "allowlist"})
    commit(root, "allowlist")
    report = run_checks(root)
    assert report["pass"]
    assert [c["environment"]["mode"] for c in report["setup"] + report["checks"]] == ["allowlist"] * 2
    for record in report["setup"] + report["checks"]:
        assert not set(record["environment"]["names"]) & {"F3_NEEDED", "F3_DEPLOY_TOKEN", "TYPESAFE_API_KEY"}


def test_evidence_records_environment_and_gate_compares_configuration(tmp_path, secrets):
    root = make_repo(tmp_path / "product")
    set_factory(root, check={"env": ["F3_NEEDED"]})
    commit(root, "allowlisted check")
    mission_id = begin(root)
    result = verify_mission(root, mission_id, "R-1")
    assert result["pass"]
    evidence = validate(root, "evidence", read_json(root, result["reference"]))
    environment = evidence["checks"][0]["environment"]
    assert environment["mode"] == "allowlist" and "F3_NEEDED" in environment["names"]
    assert "F3_DEPLOY_TOKEN" not in environment["names"]
    mission = load_mission(root, mission_id)
    assert (
        validate_verification(root, mission, load_config(root), fingerprint(root, mission))["reasons"] == []
    )

    # Evidence produced under a laxer environment than the configuration asks for is refused.
    config = load_config(root)
    config["checks"][0].pop("env")
    config["check_env"] = {"mode": "inherit"}
    reasons = validate_verification(root, mission, config, fingerprint(root, mission))["reasons"]
    assert any("environment mode allowlist differs from configured inherit" in r for r in reasons)
    config = load_config(root)
    config["checks"][0]["env"] = []
    reasons = validate_verification(root, mission, config, fingerprint(root, mission))["reasons"]
    assert any("outside its allowlist: F3_NEEDED" in r for r in reasons)


def test_gate_refuses_allowlist_config_with_inherited_or_unrecorded_evidence(tmp_path):
    root = make_repo(tmp_path / "product")
    mission_id = begin(root)
    result = verify_mission(root, mission_id, "R-1")
    assert result["checks"][0]["environment"]["mode"] == "inherit"
    mission = load_mission(root, mission_id)
    config = load_config(root)
    config["check_env"] = {"mode": "allowlist"}
    reasons = validate_verification(root, mission, config, fingerprint(root, mission))["reasons"]
    assert any("environment mode inherit differs from configured allowlist" in r for r in reasons)

    item = {"id": "unit"}
    assert environment_reasons("check", item, {"id": "unit"}, "inherit") == []
    assert environment_reasons("check", item, {"id": "unit", "env": []}, "inherit") == [
        "check unit ran without a recorded allowlist"
    ]
    leaked = {"id": "unit", "environment": {"mode": "inherit", "names": ["TYPESAFE_API_KEY"]}}
    assert "check unit received TYPESAFE_API_KEY" in environment_reasons(
        "check", leaked, {"id": "unit"}, "inherit"
    )


# Reviewers judge the diff and evidence, not the web; the planner keeps research access.


def frontmatter(path):
    return yaml.safe_load(path.read_text().split("---\n")[1])


def test_reviewer_has_no_web_tools_and_planner_keeps_them(tmp_path):
    install(tmp_path, selected="claude,copilot", skip_sync=True)
    render(tmp_path)
    claude = {
        n: frontmatter(tmp_path / f".claude/agents/factory-{n}.md")["tools"] for n in ("planner", "reviewer")
    }
    assert claude["reviewer"] == "Read, Glob, Grep, Skill"
    assert "WebFetch" in claude["planner"] and "WebSearch" in claude["planner"]
    copilot = {
        n: frontmatter(tmp_path / f".github/agents/factory-copilot-{n}.agent.md")["tools"]
        for n in ("planner", "reviewer")
    }
    assert copilot == {"planner": ["read", "search", "web"], "reviewer": ["read", "search"]}
    assert "web" not in frontmatter(tmp_path / ".github/agents/factory.agent.md")["tools"]
