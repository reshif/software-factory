"""0.3.9: the user confirms the model for each role; agent files pin it and missions are held to it."""

from __future__ import annotations

import json

import pytest

from software_factory.core import FactoryError, asset_root
from software_factory.model_roles import ROLES, binding, default_roles, map_hash, problems, stamped, status


def config(**selection):
    return {"profile": ["claude", "codex", "copilot"], "model_selection": selection}


def test_every_client_gets_a_suggested_map_from_reviewed_guidance():
    assert default_roles("claude") == {
        "orchestrator": "opus", "planner": "opus", "implementer": "sonnet", "verifier": "haiku", "reviewer": "opus",
    }  # fmt: skip
    codex, copilot = default_roles("codex"), default_roles("copilot")
    assert codex["implementer"] == "gpt-6-sol" and codex["verifier"] == "gpt-6-luna"
    assert copilot["planner"] == "Claude Opus 5.5" and set(copilot) == set(ROLES)


def test_a_map_is_confirmed_only_by_the_hash_a_proposal_stamps():
    roles = {c: default_roles(c) for c in ("claude", "codex", "copilot")}
    assert problems(config(mode="roles", roles=roles)) == [
        "the model for each role is not confirmed by the user yet"
    ]
    confirmed = stamped({"mode": "roles", "roles": roles, "confirmed_sha256": "0" * 64})
    assert confirmed["confirmed_sha256"] == map_hash(roles)
    assert problems(config(**confirmed)) == []
    edited = json.loads(json.dumps(confirmed))
    edited["roles"]["codex"]["verifier"] = "gpt-6-sol"
    assert problems(config(**edited)) == ["the model for each role is not confirmed by the user yet"]
    partial = stamped({"mode": "roles", "roles": {"claude": roles["claude"]}})
    assert problems(config(**partial)) == ["no model is chosen for every role of codex, copilot"]
    assert problems(config(mode="inherit")) == ["no model is chosen per role (model_selection is inherit)"]
    assert problems(config(mode="recommend")) == []


def test_status_offers_the_proposal_with_choices(tmp_path):
    report = status(None, config(mode="inherit"))
    assert report["confirmed"] is False and report["mode"] == "inherit"
    proposal = report["proposal"]["model_selection"]
    assert proposal["mode"] == "roles" and proposal["roles"]["claude"] == default_roles("claude")
    claude = [c["model"] for c in report["clients"]["claude"]["choices"]]
    assert {"opus", "sonnet", "haiku", "fable"} <= set(claude)
    assert all(c["good_for"] for c in report["clients"]["copilot"]["choices"])
    roles = {c: default_roles(c) for c in ("claude", "codex", "copilot")}
    settled = status(None, config(**stamped({"mode": "roles", "roles": roles})))
    assert settled["confirmed"] is True and "proposal" not in settled


def test_copilot_names_with_spaces_are_valid_configuration():
    from jsonschema import Draft7Validator

    schema = json.loads((asset_root() / "schemas/factory.schema.json").read_text())
    selection = schema["properties"]["model_selection"]
    assert not list(Draft7Validator(selection).iter_errors(stamped({"mode": "roles", "roles": {
        "copilot": {"implementer": "GPT-6 Sol (preview)"}}})))  # fmt: skip


def test_binding_records_the_map_a_mission_starts_with():
    roles = {c: default_roles(c) for c in ("claude", "codex", "copilot")}
    bound = binding(config(**stamped({"mode": "roles", "roles": roles})))
    assert bound["roles"] == roles and bound["sha256"] == map_hash(roles) and bound["confirmed"] is True
    assert binding(config(mode="inherit")) is None


@pytest.fixture
def project(tmp_path):
    from software_factory.installation import install

    install(tmp_path, selected="claude,codex,copilot", skip_sync=True, git_init=True, commit=True)
    from test_workflow import git

    git(tmp_path, "config", "user.name", "t")
    git(tmp_path, "config", "user.email", "t@example.invalid")
    return tmp_path


def test_agent_files_pin_each_clients_models(project):
    text = (project / ".claude/agents/factory-implementer.md").read_text()
    assert "\nmodel: sonnet\n" in text
    assert 'model = "gpt-6-luna"' in (project / ".codex/agents/factory-verifier.toml").read_text()
    assert (
        "model: Claude Opus 5.5" in (project / ".github/agents/factory-copilot-reviewer.agent.md").read_text()
    )
    assert "model: Claude Opus 5.5" in (project / ".github/agents/factory.agent.md").read_text()


def test_missions_wait_for_the_users_models_then_bind_them(project, monkeypatch):
    from test_mission_030 import cli, put

    from software_factory import setup_proposals
    from software_factory.core import load_config

    def roles():
        return status(project, load_config(project))

    from software_factory.workflow import assess_gate, load_mission, mission_summary

    put(project, ".factory/local/request.md", "Please add a health endpoint.\n")

    def create(id):
        return cli(project, "mission", "create", "--id", id, "--title", "Health",
                   "--request-file", ".factory/local/request.md")  # fmt: skip

    with pytest.raises(FactoryError, match="Choose the models before starting work") as refused:
        create("M-1")
    assert "models roles" in str(refused.value) and "approve S-n setup" in str(refused.value)
    proposal = roles()["proposal"]
    proposal["model_selection"]["roles"]["claude"]["implementer"] = "opus"
    proposed = setup_proposals.propose(project, proposal)
    done = setup_proposals.apply(project, proposed["proposal"], via="chat", confirm=lambda *_: None)
    assert ".claude/agents/factory-implementer.md" in done["files"]
    assert "\nmodel: opus\n" in (project / ".claude/agents/factory-implementer.md").read_text()
    assert roles()["confirmed"] is True
    assert create("M-1")["state"] == "PROPOSED"
    assert load_mission(project, "M-1")["models"]["roles"]["claude"]["implementer"] == "opus"
    assert mission_summary(load_mission(project, "M-1"))["models"]["codex"]["planner"] == "gpt-6-astra"
    # The map changes under the mission: its gate refuses it.
    changed = roles()
    assert changed["confirmed"] is True
    again = {"reason": "Use Haiku for verification everywhere", "model_selection": {
        "mode": "roles", "roles": {**load_mission(project, "M-1")["models"]["roles"],
                                   "copilot": {**default_roles("copilot"), "verifier": "Claude Haiku 4.5"}}}}  # fmt: skip
    again["model_selection"]["roles"]["claude"] = {
        **again["model_selection"]["roles"]["claude"],
        "verifier": "sonnet",
    }
    second = setup_proposals.propose(project, again)
    setup_proposals.apply(project, second["proposal"], via="chat", confirm=lambda *_: None)
    reasons = assess_gate(project, "M-1")["reasons"]
    assert any("model for each role changed since this mission started" in r for r in reasons)


def test_guard_denies_a_model_override_for_a_specialist():
    from importlib import util

    spec = util.spec_from_file_location("guard_models", asset_root() / "hooks/orchestrator_guard.py")
    guard = util.module_from_spec(spec)
    spec.loader.exec_module(guard)

    def agent(**extra):
        return {"hook_event_name": "PreToolUse", "tool_name": "Agent",
                "tool_input": {"subagent_type": "factory-implementer", "prompt": "brief", **extra}}  # fmt: skip

    guard.decide(agent())
    with pytest.raises(guard.Denied, match="may not choose a model"):
        guard.decide(agent(model="haiku"))
    guard.decide(
        {**agent(model="haiku"), "agent_id": "a1", "agent_type": "factory-planner"}
    )  # a specialist's own call
    guard.decide({"hook_event_name": "PreToolUse", "tool_name": "Bash",
                  "tool_input": {"command": ".factory/.venv/bin/software-factory models roles"}})  # fmt: skip
