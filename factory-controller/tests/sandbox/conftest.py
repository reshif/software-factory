import subprocess

import pytest


def _git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True)


@pytest.fixture
def repo_mirror(tmp_path):
    """A real local git repo under `tmp_path`, laid out as a `repos_root` mirror.

    `DockerSandbox`/`LocalSandbox.create()` clone from `repos_root/<repo with '/' -> '__'>`,
    matching how the github module would maintain local mirrors (build spec §3 B3).
    """
    repos_root = tmp_path / "repos"
    mirror = repos_root / "acme__widgets"
    mirror.mkdir(parents=True)
    _git(mirror, "init", "--quiet")
    _git(mirror, "config", "user.email", "bot@factory.test")
    _git(mirror, "config", "user.name", "factory-bot")
    (mirror / "README.md").write_text("hello\n")
    (mirror / "src").mkdir()
    (mirror / "src" / "app.py").write_text("def greet():\n    return 'hi'\n")
    (mirror / "to_delete.txt").write_text("bye\n")
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
