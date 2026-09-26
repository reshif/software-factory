import json

import httpx
import pytest
import respx

from factory.github.app_auth import AppCredentials
from factory.github.client import ForbiddenDiff, MergeConflict, RestGitHub
from factory.models import Diff
from factory.policy.action_classes import FileChange


class _FixedTokenProvider:
    """Stands in for InstallationTokenProvider so tests never touch real credentials."""

    def __init__(self, creds, *, client=None):
        self._creds = creds

    def token(self) -> str:
        return f"token-for-{self._creds.app_id}"


def make_client(*, floor, repos_root="/tmp/unused", remote_url_builder=None) -> RestGitHub:
    push_creds = AppCredentials(app_id="push-app", private_key_path="unused", installation_id="1")
    merge_creds = AppCredentials(app_id="merge-app", private_key_path="unused", installation_id="2")
    return RestGitHub(
        push_credentials=push_creds,
        merge_credentials=merge_creds,
        floor=floor,
        repos_root=repos_root,
        http_client=httpx.Client(),
        remote_url_builder=remote_url_builder,
        token_provider_factory=_FixedTokenProvider,
    )


@respx.mock
def test_get_issue_maps_fields(policy):
    respx.get("https://api.github.com/repos/acme/demo/issues/42").mock(
        return_value=httpx.Response(200, json={
            "title": "Fix the thing", "body": "steps to repro", "user": {"login": "reporter"},
            "html_url": "https://github.com/acme/demo/issues/42",
            "labels": [{"name": "factory:patch"}, "bug"],
        })
    )
    gh = make_client(floor=policy.floor)
    item = gh.get_issue("acme/demo", 42)
    assert item.item_id == "acme/demo#42"
    assert item.product == "demo"
    assert item.title == "Fix the thing"
    assert item.body == "steps to repro"
    assert item.author == "reporter"
    assert item.labels == ("factory:patch", "bug")


@respx.mock
def test_comment_issue_posts_body(policy):
    route = respx.post("https://api.github.com/repos/acme/demo/issues/42/comments").mock(
        return_value=httpx.Response(201, json={})
    )
    make_client(floor=policy.floor).comment_issue("acme/demo", 42, "hello")
    assert route.calls.last.request.headers["Authorization"] == "Bearer token-for-push-app"
    assert json.loads(route.calls.last.request.content) == {"body": "hello"}


@respx.mock
def test_mirror_labels_adds_and_removes(policy):
    remove_route = respx.delete("https://api.github.com/repos/acme/demo/issues/42/labels/old").mock(
        return_value=httpx.Response(200, json=[])
    )
    add_route = respx.post("https://api.github.com/repos/acme/demo/issues/42/labels").mock(
        return_value=httpx.Response(200, json=[])
    )
    make_client(floor=policy.floor).mirror_labels("acme/demo", 42, add=["new"], remove=["old"])
    assert remove_route.called
    assert add_route.called


@respx.mock
def test_mirror_labels_tolerates_missing_label(policy):
    respx.delete("https://api.github.com/repos/acme/demo/issues/42/labels/gone").mock(
        return_value=httpx.Response(404, json={})
    )
    # must not raise
    make_client(floor=policy.floor).mirror_labels("acme/demo", 42, remove=["gone"])


@respx.mock
def test_head_commit(policy):
    respx.get("https://api.github.com/repos/acme/demo/commits/main").mock(
        return_value=httpx.Response(200, json={"sha": "deadbeef"})
    )
    assert make_client(floor=policy.floor).head_commit("acme/demo") == "deadbeef"


@respx.mock
def test_open_pr_returns_number(policy):
    respx.post("https://api.github.com/repos/acme/demo/pulls").mock(
        return_value=httpx.Response(201, json={"number": 9})
    )
    number = make_client(floor=policy.floor).open_pr("acme/demo", head="feature/x", base="main",
                                                       title="t", body="b")
    assert number == 9


@respx.mock
def test_pr_head_sha(policy):
    respx.get("https://api.github.com/repos/acme/demo/pulls/9").mock(
        return_value=httpx.Response(200, json={"head": {"sha": "cafef00d"}})
    )
    assert make_client(floor=policy.floor).pr_head_sha("acme/demo", 9) == "cafef00d"


@respx.mock
def test_check_runs_maps_conclusions(policy):
    respx.get("https://api.github.com/repos/acme/demo/commits/deadbeef/check-runs").mock(
        return_value=httpx.Response(200, json={"check_runs": [
            {"name": "lint", "status": "completed", "conclusion": "success", "html_url": "u1"},
            {"name": "unit", "status": "completed", "conclusion": "failure", "html_url": "u2"},
            {"name": "types", "status": "in_progress", "conclusion": None, "html_url": "u3"},
            {"name": "flaky", "status": "queued", "conclusion": None, "html_url": "u4"},
            {"name": "weird", "status": "completed", "conclusion": "action_required", "html_url": "u5"},
        ]})
    )
    results = {r.name: r.conclusion for r in make_client(floor=policy.floor).check_runs("acme/demo", "deadbeef")}
    assert results == {
        "lint": "success",
        "unit": "failure",
        "types": "missing",
        "flaky": "missing",
        "weird": "failure",  # unrecognized completed conclusion fails closed
    }


@respx.mock
def test_pr_reviews(policy):
    respx.get("https://api.github.com/repos/acme/demo/pulls/9/reviews").mock(
        return_value=httpx.Response(200, json=[
            {"user": {"login": "alice"}, "state": "APPROVED", "commit_id": "c1"},
        ])
    )
    reviews = make_client(floor=policy.floor).pr_reviews("acme/demo", 9)
    assert reviews == [{"login": "alice", "state": "APPROVED", "commit_id": "c1"}]


@respx.mock
def test_merge_pr_success_uses_merge_bot_identity(policy):
    route = respx.put("https://api.github.com/repos/acme/demo/pulls/9/merge").mock(
        return_value=httpx.Response(200, json={"sha": "merged-sha"})
    )
    sha = make_client(floor=policy.floor).merge_pr("acme/demo", 9, expected_head_sha="cafef00d")
    assert sha == "merged-sha"
    assert route.calls.last.request.headers["Authorization"] == "Bearer token-for-merge-app"


@respx.mock
def test_merge_pr_conflict_on_stale_head(policy):
    respx.put("https://api.github.com/repos/acme/demo/pulls/9/merge").mock(
        return_value=httpx.Response(409, json={"message": "sha mismatch"})
    )
    with pytest.raises(MergeConflict):
        make_client(floor=policy.floor).merge_pr("acme/demo", 9, expected_head_sha="stale")


def test_push_diff_refuses_ac8_before_touching_git_or_network(policy, tmp_path):
    # A change under a forbidden path must be refused before minting a token,
    # cloning, or running any git command. repos_root points at a directory that
    # doesn't exist and the remote builder points nowhere real: if push_diff got
    # past the classification check it would blow up with a GitCommandError
    # (or hang trying to reach the network) instead of ForbiddenDiff.
    diff = Diff(base_commit="deadbeef", changes=(
        FileChange(path=".github/workflows/ci.yml", status="modified", added_lines=1, removed_lines=0),
    ), patch="not-applied")
    gh = make_client(floor=policy.floor, repos_root=str(tmp_path / "does-not-exist"),
                     remote_url_builder=lambda repo: "https://example.invalid/unreachable.git")
    with pytest.raises(ForbiddenDiff):
        gh.push_diff("acme/demo", branch="feature/x", diff=diff, message="sneaky")
