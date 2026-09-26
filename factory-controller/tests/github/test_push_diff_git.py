"""push_diff's git flow against a real local bare repo (no network, no GitHub).

The remote URL builder is injected so the client clones/pushes to a plain
filesystem path instead of https://github.com/..., which is exactly the seam
the build spec calls for testing the git flow through.
"""
import httpx
import pytest

from factory.github.app_auth import AppCredentials
from factory.github.client import ForbiddenDiff, RestGitHub
from factory.models import Diff
from factory.policy.action_classes import FileChange

from .conftest import run_git


class _FixedTokenProvider:
    def __init__(self, creds, *, client=None):
        self._creds = creds

    def token(self) -> str:
        return "unused-local-token"


@pytest.fixture
def bare_repo(tmp_path):
    """A bare repo with one commit ('base.txt') on main. Returns (path, base_sha)."""
    bare = tmp_path / "remote.git"
    run_git(["init", "--bare", "--quiet", "-b", "main", str(bare)], cwd=tmp_path)

    seed = tmp_path / "seed"
    seed.mkdir()
    run_git(["init", "--quiet", "-b", "main", str(seed)], cwd=tmp_path)
    (seed / "base.txt").write_text("base\n")
    run_git(["add", "base.txt"], cwd=seed)
    run_git(["-c", "user.name=seed", "-c", "user.email=seed@example.com", "commit", "--quiet",
             "-m", "seed"], cwd=seed)
    run_git(["remote", "add", "origin", str(bare)], cwd=seed)
    run_git(["push", "--quiet", "origin", "main"], cwd=seed)
    base_sha = run_git(["rev-parse", "HEAD"], cwd=seed).strip()
    return bare, seed, base_sha


def _capture_add_file_diff(seed_checkout, name: str, content: str) -> str:
    """Uses git itself to produce a real `git diff --cached --binary` patch."""
    (seed_checkout / name).write_text(content)
    run_git(["add", "-A"], cwd=seed_checkout)
    patch = run_git(["diff", "--cached", "--binary"], cwd=seed_checkout)
    run_git(["reset", "--quiet"], cwd=seed_checkout)
    (seed_checkout / name).unlink()
    return patch


def make_client(*, floor, repos_root, bare_path) -> RestGitHub:
    push_creds = AppCredentials(app_id="push-app", private_key_path="unused", installation_id="1")
    merge_creds = AppCredentials(app_id="merge-app", private_key_path="unused", installation_id="2")
    return RestGitHub(
        push_credentials=push_creds,
        merge_credentials=merge_creds,
        floor=floor,
        repos_root=str(repos_root),
        http_client=httpx.Client(),
        remote_url_builder=lambda repo: str(bare_path),
        token_provider_factory=_FixedTokenProvider,
        push_bot_identity=("Factory Push Bot", "push-bot@example.com"),
    )


def test_push_diff_creates_branch_with_commit(policy, tmp_path, bare_repo):
    bare, seed, base_sha = bare_repo
    patch = _capture_add_file_diff(seed, "hello.txt", "hello\n")
    diff = Diff(base_commit=base_sha,
               changes=(FileChange(path="hello.txt", status="added", added_lines=1,
                                    removed_lines=0, added_text="hello\n"),),
               patch=patch)

    gh = make_client(floor=policy.floor, repos_root=tmp_path / "work", bare_path=bare)
    sha = gh.push_diff("acme/demo", branch="feature/add-hello", diff=diff, message="add hello.txt")

    assert len(sha) == 40
    # Verify against a fresh clone of the bare repo (the push actually landed).
    verify = tmp_path / "verify"
    run_git(["clone", "--quiet", str(bare), str(verify)], cwd=tmp_path)
    run_git(["checkout", "--quiet", "feature/add-hello"], cwd=verify)
    assert (verify / "hello.txt").read_text() == "hello\n"
    assert run_git(["rev-parse", "HEAD"], cwd=verify).strip() == sha

    log = run_git(["log", "-1", "--format=%an <%ae>%n%s"], cwd=verify)
    assert log == "Factory Push Bot <push-bot@example.com>\nadd hello.txt\n"

    # main itself is untouched.
    run_git(["checkout", "--quiet", "main"], cwd=verify)
    assert not (verify / "hello.txt").exists()


def test_push_diff_leaves_no_workdir_behind(policy, tmp_path, bare_repo):
    bare, seed, base_sha = bare_repo
    patch = _capture_add_file_diff(seed, "another.txt", "content\n")
    diff = Diff(base_commit=base_sha,
               changes=(FileChange(path="another.txt", status="added", added_lines=1,
                                    added_text="content\n"),),
               patch=patch)
    work_root = tmp_path / "work"
    gh = make_client(floor=policy.floor, repos_root=work_root, bare_path=bare)
    gh.push_diff("acme/demo", branch="feature/another", diff=diff, message="add another.txt")

    assert list(work_root.iterdir()) == []


def test_push_diff_refuses_ac8_and_leaves_remote_untouched(policy, tmp_path, bare_repo):
    bare, seed, base_sha = bare_repo
    diff = Diff(base_commit=base_sha,
               changes=(FileChange(path=".github/workflows/ci.yml", status="added", added_lines=1,
                                    added_text="evil: true\n"),),
               patch="irrelevant, never applied")
    gh = make_client(floor=policy.floor, repos_root=tmp_path / "work", bare_path=bare)

    with pytest.raises(ForbiddenDiff):
        gh.push_diff("acme/demo", branch="feature/evil", diff=diff, message="sneak in a workflow")

    branches = run_git(["branch", "-a"], cwd=bare)
    assert "feature/evil" not in branches
