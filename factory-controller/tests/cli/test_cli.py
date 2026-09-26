"""`factory` CLI (build spec §3 B7 `cli.py`).

These call `main([...])` in-process (no subprocess) so fakes/monkeypatches can
sit between the CLI and the real adapters -- `factory kill-switch`/`resume`/
`unblock`/`github setup` all build a real `Factory`/GitHub client from
`Settings` if left alone, which would touch a real kit/products dir, the
network, or both.
"""
from __future__ import annotations

import json

import pytest

import factory.cli as cli
import factory.wiring as wiring


def run(monkeypatch, capsys, argv):
    exit_code = cli.main(argv)
    out = capsys.readouterr()
    return exit_code, out.out, out.err


# ── factory gates: clean errors, not tracebacks ──────────────────────────────────
def test_gates_unknown_class_is_a_clean_error_exit_2(capsys):
    exit_code, out, err = run(None, capsys, ["gates", "--class", "AC99", "--profile", "standard"])
    assert exit_code == 2
    assert "AC99" in err
    assert "Traceback" not in err


def test_gates_unknown_profile_is_argparse_exit_2(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["gates", "--class", "AC4", "--profile", "bogus"])
    assert excinfo.value.code == 2


def test_gates_unknown_level_is_argparse_exit_2(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["gates", "--class", "AC4", "--profile", "standard", "--level", "L99"])
    assert excinfo.value.code == 2


def test_gates_valid_input_still_works(capsys):
    exit_code, out, err = run(None, capsys, ["gates", "--class", "AC4", "--profile", "standard"])
    assert exit_code == 0
    payload = json.loads(out)
    assert set(payload) >= {"h1", "hm", "h2", "rules"}


# ── factory demo --help: every scenario is a listed, discoverable choice ────────
def test_demo_help_lists_scenario_names(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["demo", "--help"])
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    from factory.demo.scenarios import SCENARIOS
    for name in SCENARIOS:
        assert name in out, f"{name!r} not listed in `factory demo --help`"


def test_demo_unknown_scenario_is_a_clean_argparse_error(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["demo", "--scenario", "nonexistent"])
    assert excinfo.value.code == 2


# ── every option across the whole CLI has help text ──────────────────────────────
def test_every_option_has_help_text():
    parser = cli.build_parser()

    def check(p, path):
        for action in p._actions:
            if isinstance(action, __import__("argparse")._SubParsersAction):
                for name, sub in action.choices.items():
                    check(sub, f"{path} {name}")
                continue
            if action.dest in ("help",):
                continue
            assert action.help, f"{path}: {action.option_strings or action.dest} has no help text"

    check(parser, "factory")


# ── factory kill-switch / resume / unblock: wired to the Factory methods ────────
def test_kill_switch_calls_factory_kill_switch(monkeypatch, capsys):
    calls = []

    class FakeFactory:
        def kill_switch(self):
            calls.append("kill_switch")

    monkeypatch.setattr(wiring, "build_factory", lambda settings: FakeFactory())
    exit_code = cli.main(["kill-switch"])
    assert exit_code == 0
    assert calls == ["kill_switch"]


def test_resume_calls_factory_resume_with_mission_id(monkeypatch, capsys):
    calls = []

    class FakeFactory:
        def resume(self, mission_id):
            calls.append(mission_id)

    monkeypatch.setattr(wiring, "build_factory", lambda settings: FakeFactory())
    exit_code = cli.main(["resume", "--mission", "MIS-42"])
    assert exit_code == 0
    assert calls == ["MIS-42"]


def test_unblock_calls_factory_unblock_with_mission_id(monkeypatch, capsys):
    calls = []

    class FakeFactory:
        def unblock(self, mission_id):
            calls.append(mission_id)

    monkeypatch.setattr(wiring, "build_factory", lambda settings: FakeFactory())
    exit_code = cli.main(["unblock", "--mission", "MIS-7"])
    assert exit_code == 0
    assert calls == ["MIS-7"]


def test_resume_requires_mission_flag(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["resume"])
    assert excinfo.value.code == 2


# ── factory github setup ─────────────────────────────────────────────────────────
def test_github_setup_dry_run_prints_plan(monkeypatch, capsys):
    monkeypatch.setenv("FACTORY_MERGE_APP_ID", "999")
    exit_code, out, err = run(None, capsys, ["github", "setup", "--repo", "org/name", "--dry-run"])
    assert exit_code == 0
    plan = json.loads(out)
    assert plan["ruleset"]["bypass_actors"][0]["actor_id"] == 999
    assert plan["actions_permissions"]["default_workflow_permissions"] == "read"


def test_github_setup_without_merge_app_id_is_a_clean_error(monkeypatch, capsys):
    monkeypatch.delenv("FACTORY_MERGE_APP_ID", raising=False)
    exit_code, out, err = run(None, capsys, ["github", "setup", "--repo", "org/name", "--dry-run"])
    assert exit_code == 1
    assert "Traceback" not in err
    assert "FACTORY_MERGE_APP_ID" in err


def test_github_setup_without_dry_run_needs_push_app_credentials(monkeypatch, capsys):
    monkeypatch.setenv("FACTORY_MERGE_APP_ID", "999")
    monkeypatch.delenv("FACTORY_PUSH_APP_ID", raising=False)
    exit_code, out, err = run(None, capsys, ["github", "setup", "--repo", "org/name"])
    assert exit_code == 1
    assert "Traceback" not in err
    assert "FACTORY_PUSH_APP_ID" in err


def test_github_setup_applies_with_a_real_token_when_configured(monkeypatch, capsys):
    import factory.github.app_auth as app_auth
    import factory.github.setup as github_setup

    monkeypatch.setenv("FACTORY_MERGE_APP_ID", "999")
    monkeypatch.setenv("FACTORY_PUSH_APP_ID", "123")
    monkeypatch.setenv("FACTORY_PUSH_APP_PRIVATE_KEY_PATH", "/dev/null")
    monkeypatch.setenv("FACTORY_PUSH_APP_INSTALLATION_ID", "456")
    monkeypatch.setattr(app_auth.InstallationTokenProvider, "token", lambda self: "fake-installation-token")
    seen = {}

    def fake_apply(*, repo, merge_bot_app_id, token, api_url, dry_run):
        seen.update(repo=repo, merge_bot_app_id=merge_bot_app_id, token=token, dry_run=dry_run)
        return {"ok": True}

    monkeypatch.setattr(github_setup, "apply_repo_settings", fake_apply)
    exit_code, out, err = run(None, capsys, ["github", "setup", "--repo", "org/name"])

    assert exit_code == 0
    assert seen == {"repo": "org/name", "merge_bot_app_id": 999, "token": "fake-installation-token",
                    "dry_run": False}


# ── factory serve: never logs inbox tokens via uvicorn's access log ─────────────
def test_serve_runs_uvicorn_with_access_log_disabled(monkeypatch):
    import uvicorn
    import factory.app as app_module

    calls = {}

    def fake_run(app, *, host, port, access_log=True):
        calls.update(host=host, port=port, access_log=access_log)

    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.setattr(app_module, "create_app", lambda settings: object())
    cli.main(["serve", "--host", "127.0.0.1", "--port", "9999"])

    assert calls == {"host": "127.0.0.1", "port": 9999, "access_log": False}


def test_operator_errors_are_one_line_with_exit_2(tmp_path, monkeypatch, capsys):
    import shutil
    from factory.cli import main
    from factory.policy.loader import default_kit_dir
    shutil.copytree(default_kit_dir() / "templates" / "backend-service", tmp_path / "backend-service")
    monkeypatch.setenv("FACTORY_PRODUCTS_DIR", str(tmp_path))
    monkeypatch.delenv("FACTORY_DATABASE_URL", raising=False)
    assert main(["resume", "--mission", "MIS-missing"]) == 2
    err = capsys.readouterr().err
    assert "error: not found: MIS-missing" in err
    assert "Traceback" not in err
    monkeypatch.setenv("FACTORY_PRODUCTS_DIR", str(tmp_path / "nope"))
    assert main(["unblock", "--mission", "MIS-x"]) == 2
