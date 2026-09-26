"""GitMirror: local bare mirrors of a product repo, kept at exactly the path
`sandbox.base.CheckoutSandbox` expects to clone from (verified directly against
it below), never a `<owner>/<name>.git` convention.
"""
import os
from pathlib import Path

import pytest

from factory.github.mirror import FakeMirror, GitMirror, mirror_path
from factory.ports import NotFound
from factory.sandbox.base import CheckoutSandbox

from .conftest import run_git


class _FixedTokenProvider:
    def __init__(self, token: str = "unused-local-token"):
        self._token = token

    def token(self) -> str:
        return self._token


@pytest.fixture
def bare_remote(tmp_path):
    """A bare 'remote' repo with one commit ('a.txt') on main. Returns (bare, seed, sha)."""
    bare = tmp_path / "remote.git"
    run_git(["init", "--bare", "--quiet", "-b", "main", str(bare)], cwd=tmp_path)
    seed = tmp_path / "seed"
    seed.mkdir()
    run_git(["init", "--quiet", "-b", "main", str(seed)], cwd=tmp_path)
    (seed / "a.txt").write_text("a\n")
    run_git(["add", "a.txt"], cwd=seed)
    run_git(["-c", "user.name=seed", "-c", "user.email=seed@example.com", "commit", "--quiet",
             "-m", "first"], cwd=seed)
    run_git(["remote", "add", "origin", str(bare)], cwd=seed)
    run_git(["push", "--quiet", "origin", "main"], cwd=seed)
    sha = run_git(["rev-parse", "HEAD"], cwd=seed).strip()
    return bare, seed, sha


def make_mirror(*, repos_root, bare) -> GitMirror:
    return GitMirror(repos_root=str(repos_root), token_provider=_FixedTokenProvider(),
                     remote_url_builder=lambda repo: str(bare))


def test_mirror_path_matches_sandbox_convention(tmp_path):
    repos_root = str(tmp_path / "repos")
    sandbox = CheckoutSandbox(repos_root=repos_root, sandbox_root=str(tmp_path / "sbx"))
    assert mirror_path(repos_root, "acme/demo") == sandbox._mirror_path("acme/demo")
    assert mirror_path(repos_root, "acme/demo") == os.path.join(repos_root, "acme__demo")


def test_first_sync_clones_a_bare_mirror_at_the_sandbox_path(tmp_path, bare_remote):
    bare, seed, sha = bare_remote
    repos_root = tmp_path / "repos"
    mirror = make_mirror(repos_root=repos_root, bare=bare)

    path = mirror.sync("acme/demo")

    assert path == str(repos_root / "acme__demo")
    assert run_git(["rev-parse", "--is-bare-repository"], cwd=path).strip() == "true"
    assert run_git(["rev-parse", "main"], cwd=path).strip() == sha


def test_sandbox_can_clone_straight_from_the_synced_mirror(tmp_path, bare_remote):
    # The whole point of matching the sandbox's path convention: create()
    # must be able to find and clone from exactly what sync() returned.
    bare, seed, sha = bare_remote
    repos_root = tmp_path / "repos"
    mirror = make_mirror(repos_root=repos_root, bare=bare)
    mirror.sync("acme/demo")

    sandbox = CheckoutSandbox(repos_root=str(repos_root), sandbox_root=str(tmp_path / "sbx"))
    handle = sandbox.create("acme/demo", sha)
    try:
        assert (Path(handle.workdir) / "a.txt").read_text() == "a\n"
    finally:
        sandbox.destroy(handle)


def test_second_sync_updates_in_place_and_picks_up_a_new_commit(tmp_path, bare_remote):
    bare, seed, sha = bare_remote
    repos_root = tmp_path / "repos"
    mirror = make_mirror(repos_root=repos_root, bare=bare)
    path1 = mirror.sync("acme/demo")

    (seed / "b.txt").write_text("b\n")
    run_git(["add", "b.txt"], cwd=seed)
    run_git(["-c", "user.name=seed", "-c", "user.email=seed@example.com", "commit", "--quiet",
             "-m", "second"], cwd=seed)
    run_git(["push", "--quiet", "origin", "main"], cwd=seed)
    new_sha = run_git(["rev-parse", "HEAD"], cwd=seed).strip()

    path2 = mirror.sync("acme/demo")

    assert path2 == path1
    head_sha = run_git(["rev-parse", "main"], cwd=path2).strip()
    assert head_sha == new_sha
    assert head_sha != sha


def test_no_token_in_mirror_config_or_argv_across_clone_and_update(monkeypatch, tmp_path, bare_remote):
    import factory.gitcmd as gitcmd_module

    bare, seed, sha = bare_remote
    repos_root = tmp_path / "repos"
    token = "super-secret-mirror-token"
    mirror = GitMirror(repos_root=str(repos_root), token_provider=_FixedTokenProvider(token),
                       remote_url_builder=lambda repo: str(bare))

    calls: list[list[str]] = []
    real_run = gitcmd_module.subprocess.run

    def spying_run(cmd, **kwargs):
        calls.append(cmd)
        return real_run(cmd, **kwargs)

    monkeypatch.setattr(gitcmd_module.subprocess, "run", spying_run)

    path = mirror.sync("acme/demo")  # clone path
    mirror.sync("acme/demo")         # update path

    assert len(calls) >= 2
    for cmd in calls:
        assert not any(token in str(part) for part in cmd)

    config_text = (Path(path) / "config").read_text()
    assert token not in config_text


def test_fake_mirror_constructor_mapping():
    fake = FakeMirror({"acme/demo": "/some/local/path"})
    assert fake.sync("acme/demo") == "/some/local/path"


def test_fake_mirror_register_helper():
    fake = FakeMirror()
    fake.register("acme/demo", "/some/local/path")
    assert fake.sync("acme/demo") == "/some/local/path"


def test_fake_mirror_unknown_repo_raises_not_found():
    fake = FakeMirror()
    with pytest.raises(NotFound):
        fake.sync("acme/unknown")
