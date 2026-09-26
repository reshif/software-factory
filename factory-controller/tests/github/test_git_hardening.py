"""C3 consolidation: github/client.py hands every git call to `factory.gitcmd.run_git`
rather than running its own subprocess/hardening/redaction logic.

Argv-absence, hardening flags (hooks/fsmonitor/pager/quotePath) and token
redaction are `gitcmd`'s own responsibility and are exercised directly by
`tests/sandbox/test_gitcmd.py` (B3's suite) -- duplicating those here would
just test `gitcmd` a second time. What's specific to this module, and what
these tests cover, is the boundary: `client._git` must translate an
installation token into the HTTP Basic `x-access-token:<token>` auth header
GitHub's git-over-HTTPS actually wants (gitcmd's own default is a Bearer
header, which is right for other callers but wrong for GitHub), and must
still hand `token` through unchanged so gitcmd keeps redacting it. It also
covers the two `gitcmd.run_git` additions (`auth_header`) directly, since
that's the S1/C3 fix this gap wave made and `tests/sandbox/` isn't ours to
touch.
"""
import base64

from factory import gitcmd
from factory.github import client as client_module

TOKEN = "super-secret-installation-token"


def test_basic_auth_header_helper():
    header = client_module.basic_auth_header(TOKEN)
    assert header == f"Authorization: Basic {base64.b64encode(f'x-access-token:{TOKEN}'.encode()).decode()}"
    decoded = base64.b64decode(header.removeprefix("Authorization: Basic ")).decode()
    assert decoded == f"x-access-token:{TOKEN}"


def test_git_wrapper_sends_basic_auth_header_and_keeps_token_for_redaction(monkeypatch, tmp_path):
    captured = {}

    def fake_run_git(args, cwd=None, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        return ""

    monkeypatch.setattr(client_module.gitcmd, "run_git", fake_run_git)

    client_module._git(["status"], cwd=tmp_path, token=TOKEN)

    assert TOKEN not in " ".join(str(a) for a in captured["args"])
    assert captured["kwargs"]["token"] == TOKEN  # kept only so gitcmd keeps redacting it
    assert captured["kwargs"]["auth_header"] == client_module.basic_auth_header(TOKEN)


def test_git_wrapper_sends_no_auth_header_without_a_token(monkeypatch, tmp_path):
    captured = {}

    def fake_run_git(args, cwd=None, **kwargs):
        captured["kwargs"] = kwargs
        return ""

    monkeypatch.setattr(client_module.gitcmd, "run_git", fake_run_git)

    client_module._git(["status"], cwd=tmp_path)

    assert captured["kwargs"]["token"] is None
    assert captured["kwargs"]["auth_header"] is None


def test_gitcmd_auth_header_overrides_default_bearer(monkeypatch, tmp_path):
    import subprocess

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"], captured["env"] = cmd, kwargs["env"]
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(gitcmd.subprocess, "run", fake_run)
    gitcmd.run_git(["ls-remote", "https://example.invalid/repo.git"], cwd=str(tmp_path),
                   token="tok-SECRET-123", auth_header="Authorization: Basic AbCdEf==")

    assert not any("tok-SECRET-123" in part for part in captured["cmd"])
    assert not any("AbCdEf==" in part for part in captured["cmd"])
    assert captured["env"]["GIT_CONFIG_VALUE_0"] == "Authorization: Basic AbCdEf=="


def test_gitcmd_token_alone_still_defaults_to_bearer(monkeypatch, tmp_path):
    """Locks the existing contract sandbox/other future callers can rely on:
    passing only `token` (no `auth_header`) is unchanged by this gap wave."""
    import subprocess

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["env"] = kwargs["env"]
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(gitcmd.subprocess, "run", fake_run)
    gitcmd.run_git(["status"], cwd=str(tmp_path), token="tok-SECRET-123")

    assert captured["env"]["GIT_CONFIG_VALUE_0"] == "Authorization: Bearer tok-SECRET-123"
