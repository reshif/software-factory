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
    """Uses git itself to produce a real `git diff --cached --binary` patch.

    Deliberately does NOT pass `-c core.quotePath=false`: a non-ASCII path
    (e.g. an accented filename) comes out C-quoted in the patch header, just
    like it would from an unhardened diff capture upstream. `git apply` still
    understands its own quoting, so the file lands at the right path either
    way -- the bug this guards against is a *separate*, unquoted `FileChange`
    path that a caller might pass alongside the patch.
    """
    path = seed_checkout / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    run_git(["add", "-A"], cwd=seed_checkout)
    patch = run_git(["diff", "--cached", "--binary"], cwd=seed_checkout)
    run_git(["reset", "--quiet"], cwd=seed_checkout)
    path.unlink()
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


# ── R-A2 backstop: never trust diff.changes alone ───────────────────────────
#
# A real bug: git C-quotes non-ASCII paths in a patch header by default, and
# an upstream diff-capture step that doesn't unquote them can hand push_diff
# a `FileChange.path` that no longer matches a forbidden-path glob, even
# though the patch itself still stages the real (forbidden) file. Each test
# below builds a real patch for a forbidden path, then pairs it with a
# `FileChange` whose recorded path is wrong/unquoted -- exactly what the
# upstream bug produces -- and asserts push_diff still refuses it, because
# it re-derives the real staged paths from git itself after `git apply`.

@pytest.mark.parametrize("real_path, decoy_path", [
    # decoy_path mimics what a naive parser gets from git's default (quoted,
    # non -z) --name-status output for a non-ASCII path: the literal quote
    # characters and octal escapes, unstripped -- so it no longer starts
    # with ".github/" or "policies/" and slips past a glob match.
    (".github/workflows/évil.yml", "\".github/workflows/\\303\\251vil.yml\""),
    ("policies/é.yaml", "\"policies/\\303\\251.yaml\""),
])
def test_push_diff_backstop_catches_forbidden_path_diff_changes_missed(
    policy, tmp_path, bare_repo, real_path, decoy_path,
):
    bare, seed, base_sha = bare_repo
    patch = _capture_add_file_diff(seed, real_path, "evil: true\n")
    # The decoy path is deliberately NOT the real one and matches no forbidden
    # glob, simulating a mis-unquoted upstream FileChange.
    diff = Diff(base_commit=base_sha, changes=(
        FileChange(path=decoy_path, status="added", added_lines=1, added_text="evil: true\n"),
    ), patch=patch)
    gh = make_client(floor=policy.floor, repos_root=tmp_path / "work", bare_path=bare)

    with pytest.raises(ForbiddenDiff):
        gh.push_diff("acme/demo", branch="feature/evil", diff=diff, message="sneak in a forbidden file")

    branches = run_git(["branch", "-a"], cwd=bare)
    assert "feature/evil" not in branches
    assert list((tmp_path / "work").iterdir()) == []


def test_push_diff_backstop_uses_product_forbidden_paths(policy, tmp_path, bare_repo):
    # prod.env isn't covered by the shared floor's forbidden globs (those only
    # cover *.env, i.e. a dotfile), but a product can forbid it explicitly.
    bare, seed, base_sha = bare_repo
    patch = _capture_add_file_diff(seed, "prod.env", "SECRET=1\n")
    # Same upstream-mismatch scenario as above: the recorded FileChange path
    # doesn't even mention the real file, so only the backstop's re-derived
    # real path, checked against product_forbidden, catches it.
    diff = Diff(base_commit=base_sha, changes=(
        FileChange(path="unrelated.txt", status="added", added_lines=1, added_text="SECRET=1\n"),
    ), patch=patch)
    gh = make_client(floor=policy.floor, repos_root=tmp_path / "work", bare_path=bare)

    with pytest.raises(ForbiddenDiff):
        gh.push_diff("acme/demo", branch="feature/leak", diff=diff, message="leak prod secrets",
                     product_forbidden=("**/*.env",))

    branches = run_git(["branch", "-a"], cwd=bare)
    assert "feature/leak" not in branches


def test_push_diff_product_protected_does_not_block_ac6(policy, tmp_path, bare_repo):
    # product_protected paths raise the class to AC6 (needs approval) but do
    # NOT block the push outright -- only AC8 (product_forbidden or the
    # floor) does that. This distinguishes the two keyword args.
    bare, seed, base_sha = bare_repo
    patch = _capture_add_file_diff(seed, "src/billing/ledger.py", "x = 1\n")
    diff = Diff(base_commit=base_sha, changes=(
        FileChange(path="src/billing/ledger.py", status="added", added_lines=1, added_text="x = 1\n"),
    ), patch=patch)
    gh = make_client(floor=policy.floor, repos_root=tmp_path / "work", bare_path=bare)

    sha = gh.push_diff("acme/demo", branch="feature/billing", diff=diff, message="add ledger",
                       product_protected=("src/billing/**",))
    assert len(sha) == 40
