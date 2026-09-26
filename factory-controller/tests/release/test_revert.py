from factory.github.fake import FakeGitHub
from factory.release.revert import auto_revert


def test_auto_revert_calls_github_and_returns_sha(policy):
    github = FakeGitHub(floor=policy.floor)
    revert_sha = auto_revert(github, "acme/app", "deadbeef")
    assert revert_sha == github.revert_commit("acme/app", "deadbeef")


def test_auto_revert_updates_the_default_branch_head(policy):
    github = FakeGitHub(floor=policy.floor)
    head_before = github.head_commit("acme/app", "main")
    revert_sha = auto_revert(github, "acme/app", head_before)
    assert github.head_commit("acme/app", "main") == revert_sha
    assert revert_sha != head_before
