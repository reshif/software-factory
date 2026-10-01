"""Chat approvals: the user's own `approve <MISSION> <kind>` message, recorded by a Claude Code hook."""

from __future__ import annotations

import importlib.util
import io
import json

import pytest
from test_mission_030 import ASSESSMENT, create, plan_mission, put, repo  # noqa: F401  (fixture)

from software_factory.core import asset_root, hash_file
from software_factory.installation import install, uninstall
from software_factory.rendering import CLAUDE_SETTINGS, RUNTIME_DENY, chat_approval_entry, guard_entry, render
from software_factory.workflow import load_mission

USER_SETTINGS = {
    "permissions": {"allow": ["Bash(npm test)"]},
    "hooks": {"UserPromptSubmit": [{"hooks": [{"type": "command", "command": "echo mine"}]}]},
}


def settings(root):
    return json.loads((root / CLAUDE_SETTINGS).read_text())


def hook_module():
    spec = importlib.util.spec_from_file_location("chat_approval", asset_root() / "hooks/chat_approval.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_hook(root, prompt, event="UserPromptSubmit"):
    out = io.StringIO()
    payload = {"hook_event_name": event, "session_id": "S-1", "prompt": prompt}
    assert hook_module().main([str(root)], io.StringIO(json.dumps(payload)), out) == 0
    text = out.getvalue()
    return json.loads(text)["hookSpecificOutput"]["additionalContext"] if text else None


# Installation of the hook entry.


def test_claude_install_adds_the_chat_approval_hook(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    assert settings(tmp_path) == {
        "hooks": {"UserPromptSubmit": [chat_approval_entry()], "PreToolUse": [guard_entry()]},
        "permissions": {"deny": list(RUNTIME_DENY)},
    }
    record = json.loads((tmp_path / "factory.lock.json").read_text())["generated"][CLAUDE_SETTINGS]
    assert record["kind"] == "json" and record["existed"] is False and record["deny"] == sorted(RUNTIME_DENY)
    assert render(tmp_path, check=True)["ok"]


def test_existing_claude_settings_are_kept_and_restored_on_uninstall(tmp_path):
    put(tmp_path, CLAUDE_SETTINGS, USER_SETTINGS)
    install(tmp_path, selected="claude", skip_sync=True)
    merged = settings(tmp_path)
    assert merged["permissions"] == {**USER_SETTINGS["permissions"], "deny": list(RUNTIME_DENY)}
    assert merged["hooks"]["UserPromptSubmit"] == [
        *USER_SETTINGS["hooks"]["UserPromptSubmit"],
        chat_approval_entry(),
    ]
    render(tmp_path)  # Idempotent: the entry and the deny rules are not added twice.
    assert settings(tmp_path)["hooks"]["UserPromptSubmit"].count(chat_approval_entry()) == 1
    assert settings(tmp_path)["permissions"]["deny"] == list(RUNTIME_DENY)
    uninstall(tmp_path)
    assert settings(tmp_path) == USER_SETTINGS


def test_disabling_chat_approvals_removes_only_the_factory_entry(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    config = json.loads((tmp_path / "factory.json").read_text())
    config["approvals"] = {"chat": False}
    (tmp_path / "factory.json").write_text(json.dumps(config, indent=2) + "\n")
    render(tmp_path)
    assert "UserPromptSubmit" not in settings(tmp_path)["hooks"]  # The orchestrator guard stays.
    assert settings(tmp_path)["hooks"]["PreToolUse"] == [guard_entry()]
    config["enforcement"] = {"claude_orchestrator_agent": False}
    (tmp_path / "factory.json").write_text(json.dumps(config, indent=2) + "\n")
    render(tmp_path)
    assert not (tmp_path / CLAUDE_SETTINGS).exists()  # The factory created it, so it goes entirely.
    assert CLAUDE_SETTINGS not in json.loads((tmp_path / "factory.lock.json").read_text())["generated"]


def test_codex_only_install_has_no_claude_settings(tmp_path):
    install(tmp_path, selected="codex", skip_sync=True)
    assert not (tmp_path / CLAUDE_SETTINGS).exists()


def test_invalid_user_settings_are_refused_not_overwritten(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / CLAUDE_SETTINGS).write_text("{not json")
    with pytest.raises(Exception, match="not valid JSON"):
        install(tmp_path, selected="claude", skip_sync=True)
    assert (tmp_path / CLAUDE_SETTINGS).read_text() == "{not json"


# Recording from the user's message.


def test_chat_approval_records_scope_bound_to_the_current_spec(repo):  # noqa: F811
    create(repo, "M-CHAT", "patch")
    put(repo, ".factory/missions/M-CHAT/spec.md", "# Spec\n\nChange VALUE to 2.\n")
    put(repo, ".factory/missions/M-CHAT/assessment.md", ASSESSMENT)
    note = run_hook(repo, "Looks right.\napprove M-CHAT scope please go ahead")
    decision = load_mission(repo, "M-CHAT")["decisions"][-1]
    spec_hash = hash_file(repo, ".factory/missions/M-CHAT/spec.md")
    assert decision["kind"] == "scope" and decision["subject_hash"] == spec_hash
    assert decision["id"] == f"D-SCOPE-{spec_hash[:8]}"
    assert (
        "Claude Code chat (session S-1" in decision["reference"]
        and "approve M-CHAT scope" in decision["reference"]
    )
    assert "recorded the user's scope approval for M-CHAT" in note and "accept-scope" in note


def test_only_user_prompt_events_are_recorded(repo):  # noqa: F811
    create(repo, "M-CHAT", "patch")
    put(repo, ".factory/missions/M-CHAT/spec.md", "# Spec\n")
    put(repo, ".factory/missions/M-CHAT/assessment.md", ASSESSMENT)
    assert run_hook(repo, "approve M-CHAT scope", event="PreToolUse") is None
    assert run_hook(repo, "I think we could approve M-CHAT scope later") is None  # Not at a line start.
    assert load_mission(repo, "M-CHAT")["decisions"] == []


def test_unknown_mission_and_missing_ci_are_reported_not_recorded(repo):  # noqa: F811
    id = plan_mission(repo)
    before = len(load_mission(repo, id)["decisions"])
    assert "could not record the user's scope approval for M-NOPE" in run_hook(repo, "approve M-NOPE scope")
    note = run_hook(repo, f"approve {id} merge")
    assert "Record the successful CI result first" in note and "do not record it yourself" in note
    assert len(load_mission(repo, id)["decisions"]) == before


def test_repeated_approval_is_reported_as_already_recorded(repo):  # noqa: F811
    create(repo, "M-CHAT", "patch")
    put(repo, ".factory/missions/M-CHAT/spec.md", "# Spec\n")
    put(repo, ".factory/missions/M-CHAT/assessment.md", ASSESSMENT)
    run_hook(repo, "approve M-CHAT scope")
    assert "Duplicate decision ID" in run_hook(repo, "approve M-CHAT scope")
    assert len(load_mission(repo, "M-CHAT")["decisions"]) == 1


def test_a_users_own_deny_rule_survives_uninstall(tmp_path):
    own = {"permissions": {"deny": ["Read(./secrets/**)", "Read(./.factory/src/**)"]}}
    put(tmp_path, CLAUDE_SETTINGS, own)
    install(tmp_path, selected="claude", skip_sync=True)
    assert settings(tmp_path)["permissions"]["deny"] == [
        *own["permissions"]["deny"],
        "Read(./.factory/.venv/**)",
    ]
    uninstall(tmp_path)
    assert settings(tmp_path) == own  # the user's identical rule stays: the factory owns only what it added
