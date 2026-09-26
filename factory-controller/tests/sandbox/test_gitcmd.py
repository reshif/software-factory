"""Unit tests for the shared hardened git runner (src/factory/gitcmd.py)."""
import os

import pytest

from factory import gitcmd


def test_run_git_returns_stdout(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    gitcmd.run_git(["init", "-q"], cwd=str(repo))
    out = gitcmd.run_git(["rev-parse", "--is-bare-repository"], cwd=str(repo))
    assert out.strip() == "false"


def test_run_git_raises_git_error_on_nonzero_exit(tmp_path):
    with pytest.raises(gitcmd.GitError):
        gitcmd.run_git(["not-a-real-git-subcommand"], cwd=str(tmp_path))


def test_run_git_times_out(tmp_path, monkeypatch):
    import subprocess

    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="git", timeout=kwargs.get("timeout", 0), output="", stderr="hung")

    monkeypatch.setattr(gitcmd.subprocess, "run", fake_run)
    with pytest.raises(gitcmd.GitError, match="timed out"):
        gitcmd.run_git(["status"], cwd=str(tmp_path), timeout=0.01)


def test_run_git_redacts_token_from_error_message(tmp_path):
    secret = "sk-super-secret-token-value"
    try:
        gitcmd.run_git(["not-a-real-git-subcommand"], cwd=str(tmp_path), token=secret)
        pytest.fail("expected GitError")
    except gitcmd.GitError as exc:
        assert secret not in str(exc)


def test_run_git_ignores_ambient_git_config(tmp_path, monkeypatch):
    """GIT_CONFIG_GLOBAL/SYSTEM are forced to /dev/null regardless of what's already set:
    a repo with no local user.name must NOT fall back to a poisoned global config."""
    poisoned_global = tmp_path / "poisoned-gitconfig"
    poisoned_global.write_text("[user]\n\tname = should-never-be-read\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(poisoned_global))

    repo = tmp_path / "repo"
    repo.mkdir()
    gitcmd.run_git(["init", "-q"], cwd=str(repo))
    with pytest.raises(gitcmd.GitError):
        # exits non-zero ("key not found") precisely because the poisoned global,
        # which WOULD have satisfied this lookup, was never consulted.
        gitcmd.run_git(["config", "--get", "user.name"], cwd=str(repo))


def test_run_git_overrides_a_hostile_repo_config(tmp_path):
    """A repo's own `.git/config` (as an attacker-planted one would) sets core.pager to
    run a command; our hardened `-c` flags outrank config files, so it never runs."""
    repo = tmp_path / "repo"
    repo.mkdir()
    gitcmd.run_git(["init", "-q"], cwd=str(repo))
    gitcmd.run_git(["config", "user.email", "a@a.com"], cwd=str(repo))
    gitcmd.run_git(["config", "user.name", "a"], cwd=str(repo))
    (repo / "f.txt").write_text("hello\n")
    gitcmd.run_git(["add", "-A"], cwd=str(repo))
    gitcmd.run_git(["commit", "-q", "-m", "init"], cwd=str(repo))

    marker = tmp_path / "PAGER_RAN"
    with open(repo / ".git" / "config", "a") as f:
        f.write(f"\n[core]\n\tpager = touch {marker} && cat\n")

    out = gitcmd.run_git(["log", "--oneline", "-1"], cwd=str(repo))
    assert "init" in out
    assert not marker.exists()


def test_run_git_disables_hooks_from_a_hostile_repo_config(tmp_path):
    """A repo's own config points core.hooksPath at a directory with a pre-commit hook
    that would otherwise run arbitrary code on `git commit`; our -c override stops it."""
    repo = tmp_path / "repo"
    repo.mkdir()
    gitcmd.run_git(["init", "-q"], cwd=str(repo))
    gitcmd.run_git(["config", "user.email", "a@a.com"], cwd=str(repo))
    gitcmd.run_git(["config", "user.name", "a"], cwd=str(repo))

    hooks_dir = repo / "evil-hooks"
    hooks_dir.mkdir()
    marker = tmp_path / "HOOK_RAN"
    hook = hooks_dir / "pre-commit"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    os.chmod(hook, 0o755)
    with open(repo / ".git" / "config", "a") as f:
        f.write(f"\n[core]\n\thooksPath = {hooks_dir}\n")

    (repo / "f.txt").write_text("hello\n")
    gitcmd.run_git(["add", "-A"], cwd=str(repo))
    gitcmd.run_git(["commit", "-q", "-m", "init"], cwd=str(repo))

    assert not marker.exists()


def test_token_never_appears_on_argv(monkeypatch, tmp_path):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"], seen["env"] = cmd, kwargs["env"]
        import subprocess
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(gitcmd.subprocess, "run", fake_run)
    gitcmd.run_git(["ls-remote", "https://example.invalid/repo.git"], cwd=str(tmp_path), token="tok-SECRET-123")
    assert not any("tok-SECRET-123" in part for part in seen["cmd"])
    assert seen["env"]["GIT_CONFIG_VALUE_0"] == "Authorization: Bearer tok-SECRET-123"
