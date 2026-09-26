import hashlib
import hmac
import json

from factory.github.webhooks import (CheckSuiteCompleted, IssueCommentCreated, IssueLabeled,
                                      PullRequestReview, PushToDefault, verify_signature, parse_event)

SECRET = "s3cr3t"


def sign(body: bytes, secret: str = SECRET) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def test_valid_signature_verifies():
    body = b'{"hello": "world"}'
    assert verify_signature(body, sign(body), SECRET) is True


def test_tampered_body_fails():
    body = b'{"hello": "world"}'
    tampered = b'{"hello": "mallory"}'
    assert verify_signature(tampered, sign(body), SECRET) is False


def test_missing_signature_fails():
    body = b'{"hello": "world"}'
    assert verify_signature(body, None, SECRET) is False


def test_wrong_scheme_fails():
    body = b"payload"
    assert verify_signature(body, "sha1=" + hashlib.sha1(body).hexdigest(), SECRET) is False


def test_empty_secret_fails_closed():
    body = b"payload"
    assert verify_signature(body, sign(body, secret=""), "") is False


def test_issue_labeled():
    payload = {
        "action": "labeled",
        "repository": {"full_name": "acme/demo"},
        "label": {"name": "factory:patch"},
        "issue": {"number": 42, "title": "<script>bad</script>", "body": "do the thing",
                  "user": {"login": "reporter"}},
    }
    event = parse_event("issues", payload)
    assert event == IssueLabeled(repo="acme/demo", number=42, label="factory:patch",
                                  title="<script>bad</script>", body="do the thing", author="reporter")


def test_issue_labeled_wrong_action_ignored():
    payload = {"action": "closed", "repository": {"full_name": "acme/demo"},
               "issue": {"number": 1, "user": None}}
    assert parse_event("issues", payload) is None


def test_pull_request_review_submitted():
    payload = {
        "action": "submitted",
        "repository": {"full_name": "acme/demo"},
        "pull_request": {"number": 7},
        "review": {"user": {"login": "reviewer1"}, "state": "approved", "commit_id": "abc123"},
    }
    event = parse_event("pull_request_review", payload)
    assert event == PullRequestReview(repo="acme/demo", number=7, reviewer="reviewer1",
                                       state="APPROVED", commit_id="abc123")


def test_check_suite_completed():
    payload = {
        "action": "completed",
        "repository": {"full_name": "acme/demo"},
        "check_suite": {"head_sha": "deadbeef", "conclusion": "success"},
    }
    event = parse_event("check_suite", payload)
    assert event == CheckSuiteCompleted(repo="acme/demo", sha="deadbeef", conclusion="success")


def test_push_to_default_branch():
    payload = {
        "ref": "refs/heads/main",
        "after": "cafef00d",
        "pusher": {"name": "factory-push-bot"},
        "repository": {"full_name": "acme/demo", "default_branch": "main"},
    }
    event = parse_event("push", payload)
    assert event == PushToDefault(repo="acme/demo", branch="main", after="cafef00d",
                                   pusher="factory-push-bot")


def test_push_to_non_default_branch_ignored():
    payload = {
        "ref": "refs/heads/feature/x",
        "after": "cafef00d",
        "repository": {"full_name": "acme/demo", "default_branch": "main"},
    }
    assert parse_event("push", payload) is None


def test_issue_comment_created():
    payload = {
        "action": "created",
        "repository": {"full_name": "acme/demo"},
        "issue": {"number": 3},
        "comment": {"body": "please revise", "user": {"login": "approver"}},
    }
    event = parse_event("issue_comment", payload)
    assert event == IssueCommentCreated(repo="acme/demo", number=3, body="please revise",
                                         author="approver")


def test_unknown_event_ignored():
    assert parse_event("star", {"action": "created"}) is None


def test_unknown_action_on_known_event_ignored():
    payload = {"action": "reopened", "repository": {"full_name": "acme/demo"},
               "issue": {"number": 1, "user": None}}
    assert parse_event("issues", payload) is None
