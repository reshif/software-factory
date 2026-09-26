import pytest

from factory.github.client import ForbiddenDiff, MergeConflict
from factory.github.fake import FakeGitHub
from factory.models import CheckResult, Diff, WorkItem
from factory.policy.action_classes import FileChange
from factory.ports import NotFound


def make_diff(path="src/x.py", added_text="x = 1\n") -> Diff:
    return Diff(base_commit="deadbeef",
               changes=(FileChange(path=path, status="added", added_lines=1, added_text=added_text),),
               patch="unused-by-fake")


def test_issues_round_trip(policy):
    gh = FakeGitHub(floor=policy.floor)
    item = WorkItem(item_id="acme/demo#1", product="demo", repo="acme/demo", number=1,
                    title="t", body="b")
    gh.add_issue(item)
    assert gh.get_issue("acme/demo", 1) == item

    gh.comment_issue("acme/demo", 1, "hi")
    assert gh.comments("acme/demo", 1) == ["hi"]

    gh.mirror_labels("acme/demo", 1, add=["a", "b"])
    assert gh.labels("acme/demo", 1) == {"a", "b"}
    gh.mirror_labels("acme/demo", 1, add=["c"], remove=["a"])
    assert gh.labels("acme/demo", 1) == {"b", "c"}


def test_head_commit_is_stable_and_repo_scoped(policy):
    gh = FakeGitHub(floor=policy.floor)
    first = gh.head_commit("acme/demo", "main")
    assert gh.head_commit("acme/demo", "main") == first
    assert gh.head_commit("acme/other", "main") != first


def test_push_diff_advances_branch(policy):
    gh = FakeGitHub(floor=policy.floor)
    before = gh.head_commit("acme/demo", "feature/x")
    sha = gh.push_diff("acme/demo", branch="feature/x", diff=make_diff(), message="add x")
    assert sha != before
    assert gh.head_commit("acme/demo", "feature/x") == sha


def test_push_diff_refuses_ac8(policy):
    gh = FakeGitHub(floor=policy.floor)
    before = gh.head_commit("acme/demo", "feature/evil")
    diff = make_diff(path=".github/workflows/ci.yml", added_text="evil: true\n")
    with pytest.raises(ForbiddenDiff):
        gh.push_diff("acme/demo", branch="feature/evil", diff=diff, message="sneak")
    # the branch was never advanced by the refused push
    assert gh.head_commit("acme/demo", "feature/evil") == before


def test_open_pr_and_merge_happy_path(policy):
    gh = FakeGitHub(floor=policy.floor)
    sha = gh.push_diff("acme/demo", branch="feature/x", diff=make_diff(), message="add x")
    number = gh.open_pr("acme/demo", head="feature/x", base="main", title="t", body="b")
    assert gh.pr_head_sha("acme/demo", number) == sha

    merge_sha = gh.merge_pr("acme/demo", number, expected_head_sha=sha)
    assert merge_sha != sha
    assert gh.head_commit("acme/demo", "main") == merge_sha
    assert gh.pr(number=number, repo="acme/demo").merged is True


def test_merge_pr_rejects_stale_head(policy):
    gh = FakeGitHub(floor=policy.floor)
    sha = gh.push_diff("acme/demo", branch="feature/x", diff=make_diff(), message="add x")
    number = gh.open_pr("acme/demo", head="feature/x", base="main", title="t", body="b")
    with pytest.raises(MergeConflict):
        gh.merge_pr("acme/demo", number, expected_head_sha="not-the-real-sha")
    assert gh.pr(repo="acme/demo", number=number).merged is False


def test_check_runs_default_empty_and_settable(policy):
    gh = FakeGitHub(floor=policy.floor)
    assert gh.check_runs("acme/demo", "deadbeef") == []
    checks = [CheckResult(name="lint", conclusion="success")]
    gh.set_checks("acme/demo", "deadbeef", checks)
    assert gh.check_runs("acme/demo", "deadbeef") == checks


def test_reviews_default_empty_and_settable(policy):
    gh = FakeGitHub(floor=policy.floor)
    assert gh.pr_reviews("acme/demo", 1) == []
    gh.add_review("acme/demo", 1, login="alice", state="APPROVED", commit_id="c1")
    assert gh.pr_reviews("acme/demo", 1) == [{"login": "alice", "state": "APPROVED", "commit_id": "c1"}]


def test_revert_commit_advances_branch(policy):
    gh = FakeGitHub(floor=policy.floor)
    sha = gh.push_diff("acme/demo", branch="main", diff=make_diff(), message="add x")
    revert_sha = gh.revert_commit("acme/demo", sha, branch="main")
    assert revert_sha != sha
    assert gh.head_commit("acme/demo", "main") == revert_sha


def test_push_diff_uses_product_forbidden_paths(policy):
    gh = FakeGitHub(floor=policy.floor)
    diff = make_diff(path="prod.env", added_text="SECRET=1\n")
    with pytest.raises(ForbiddenDiff):
        gh.push_diff("acme/demo", branch="feature/leak", diff=diff, message="leak",
                     product_forbidden=("**/*.env",))


def test_push_diff_uses_product_protected_paths_without_blocking(policy):
    gh = FakeGitHub(floor=policy.floor)
    diff = make_diff(path="src/billing/ledger.py", added_text="x = 1\n")
    # product_protected raises the class to AC6 (approval), not AC8: it must
    # not be refused outright the way a forbidden path is.
    sha = gh.push_diff("acme/demo", branch="feature/billing", diff=diff, message="add ledger",
                       product_protected=("src/billing/**",))
    assert gh.head_commit("acme/demo", "feature/billing") == sha


def test_get_issue_raises_not_found_for_unknown_issue(policy):
    gh = FakeGitHub(floor=policy.floor)
    with pytest.raises(NotFound):
        gh.get_issue("acme/demo", 999)


def test_comment_and_mirror_labels_raise_not_found_for_unknown_issue(policy):
    gh = FakeGitHub(floor=policy.floor)
    with pytest.raises(NotFound):
        gh.comment_issue("acme/demo", 999, "hi")
    with pytest.raises(NotFound):
        gh.mirror_labels("acme/demo", 999, add=["x"])


def test_pr_head_sha_and_merge_raise_not_found_for_unknown_pr(policy):
    gh = FakeGitHub(floor=policy.floor)
    with pytest.raises(NotFound):
        gh.pr_head_sha("acme/demo", 999)
    with pytest.raises(NotFound):
        gh.merge_pr("acme/demo", 999, expected_head_sha="whatever")
    with pytest.raises(NotFound):
        gh.pr("acme/demo", 999)
