"""S1: the installation token must never appear on a git subprocess's argv.

`/proc/<pid>/cmdline` (and `ps`) are readable by anyone who can see the
process on a shared box, so a `-c http.extraHeader=...<token>...` argv
element is a real leak even though it's not written to disk. This tests
`_run_git`/`_git_env` directly by monkeypatching `subprocess.run` and
inspecting exactly what it was called with -- no real git process needed.
"""
from pathlib import Path

import pytest

from factory.github import client as client_module

TOKEN = "super-secret-installation-token"


class _FakeCompletedProcess:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_token_absent_from_argv_present_only_via_env(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return _FakeCompletedProcess()

    monkeypatch.setattr(client_module.subprocess, "run", fake_run)

    client_module._run_git(["status"], cwd=tmp_path, token=TOKEN)

    argv_text = " ".join(captured["cmd"])
    assert TOKEN not in argv_text
    # The token isn't raw anywhere either (it travels base64-encoded, as a
    # Basic-auth value), but it must specifically be out of argv and in env.
    assert "-c" not in captured["cmd"], "config must not be passed as -c argv"
    env = captured["env"]
    assert env is not None
    count = int(env["GIT_CONFIG_COUNT"])
    values = [env[f"GIT_CONFIG_VALUE_{i}"] for i in range(count)]
    assert any("basic" in v.lower() and "AUTHORIZATION" in v.upper() for v in values)


def test_hardening_config_always_present(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs.get("env")
        return _FakeCompletedProcess()

    monkeypatch.setattr(client_module.subprocess, "run", fake_run)

    client_module._run_git(["status"], cwd=tmp_path)

    env = captured["env"]
    count = int(env["GIT_CONFIG_COUNT"])
    keys = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(count)}
    assert keys["core.hooksPath"] == "/dev/null"
    assert keys["core.fsmonitor"] == "false"
    assert env["GIT_CONFIG_GLOBAL"] == "/dev/null"


def test_extra_config_merged_without_argv_leak(monkeypatch, tmp_path):
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env")
        return _FakeCompletedProcess()

    monkeypatch.setattr(client_module.subprocess, "run", fake_run)

    client_module._run_git(["commit", "-m", "msg"], cwd=tmp_path,
                           config={"user.name": "Bot", "user.email": "bot@example.com"})

    assert "-c" not in captured["cmd"]
    env = captured["env"]
    count = int(env["GIT_CONFIG_COUNT"])
    keys = {env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"] for i in range(count)}
    assert keys["user.name"] == "Bot"
    assert keys["user.email"] == "bot@example.com"


def test_error_message_redacts_token(monkeypatch, tmp_path):
    def fake_run(cmd, **kwargs):
        return _FakeCompletedProcess(returncode=1, stderr=f"fatal: auth failed for token {TOKEN}")

    monkeypatch.setattr(client_module.subprocess, "run", fake_run)

    with pytest.raises(client_module.GitCommandError) as excinfo:
        client_module._run_git(["push"], cwd=tmp_path, token=TOKEN)

    assert TOKEN not in str(excinfo.value)
