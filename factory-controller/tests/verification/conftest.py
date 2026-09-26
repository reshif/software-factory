import subprocess

import pytest


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def repo_mirror(tmp_path):
    """A real local git repo under `tmp_path`, laid out as a `repos_root` mirror.

    Duplicated from `tests/sandbox/conftest.py` (fixtures aren't shared across sibling
    test packages) so `LocalSandbox` can be exercised here for the R-A7 check-gaming
    regression test without reaching into another module's test tree.
    """
    repos_root = tmp_path / "repos"
    mirror = repos_root / "acme__widgets"
    mirror.mkdir(parents=True)
    _git(mirror, "init", "--quiet")
    _git(mirror, "config", "user.email", "bot@factory.test")
    _git(mirror, "config", "user.name", "factory-bot")
    (mirror / "README.md").write_text("hello\n")
    _git(mirror, "add", "-A")
    _git(mirror, "commit", "--quiet", "-m", "init")
    base_commit = subprocess.run(
        ["git", "-C", str(mirror), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()
    return {
        "repos_root": str(repos_root),
        "sandbox_root": str(tmp_path / "sandboxes"),
        "repo": "acme/widgets",
        "base_commit": base_commit,
    }
