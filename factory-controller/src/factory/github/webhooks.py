"""GitHub webhook verification and typed event parsing (final draft §11, §12.2 M3).

Signatures are verified with a constant-time compare (`hmac.compare_digest`).
Payloads are parsed into typed events; everything else is explicitly ignored
so the pipeline never has to guess what an unrecognized payload means.
Issue/PR text carried on these events is untrusted data (§13.1 #6): it is
never executed and callers must escape it before rendering it as HTML.
"""
import hashlib
import hmac
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-Hub-Signature-256"


def verify_signature(payload: bytes, signature: str | None, secret: str) -> bool:
    """Verifies an `X-Hub-Signature-256` header against the raw request body.

    Returns False (never raises) for a missing header, wrong scheme or mismatch,
    using a constant-time compare so timing can't leak how much of the digest matched.
    """
    if not signature or not secret:
        return False
    if not signature.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


@dataclass(frozen=True)
class IssueLabeled:
    repo: str
    number: int
    label: str
    title: str
    body: str
    author: str | None


@dataclass(frozen=True)
class PullRequestReview:
    repo: str
    number: int
    reviewer: str
    state: str            # APPROVED | CHANGES_REQUESTED | COMMENTED | DISMISSED
    commit_id: str


@dataclass(frozen=True)
class CheckSuiteCompleted:
    repo: str
    sha: str
    conclusion: str | None


@dataclass(frozen=True)
class PushToDefault:
    repo: str
    branch: str
    after: str
    pusher: str | None


@dataclass(frozen=True)
class IssueCommentCreated:
    repo: str
    number: int
    body: str
    author: str | None


WebhookEvent = IssueLabeled | PullRequestReview | CheckSuiteCompleted | PushToDefault | IssueCommentCreated


def _repo_full_name(payload: dict) -> str:
    return payload["repository"]["full_name"]


def _user_login(node: dict | None) -> str | None:
    return node["login"] if node else None


def parse_event(event: str, payload: dict[str, Any]) -> WebhookEvent | None:
    """Parses a GitHub webhook delivery into a typed event, or None if it's ignored.

    `event` is the `X-GitHub-Event` header value. Unrecognized event types or
    actions are explicitly ignored (not an error) rather than raising.
    """
    action = payload.get("action")

    if event == "issues" and action == "labeled":
        issue = payload["issue"]
        label = payload.get("label", {}).get("name", "")
        return IssueLabeled(
            repo=_repo_full_name(payload),
            number=issue["number"],
            label=label,
            title=issue.get("title", ""),
            body=issue.get("body") or "",
            author=_user_login(issue.get("user")),
        )

    if event == "pull_request_review" and action == "submitted":
        review = payload["review"]
        return PullRequestReview(
            repo=_repo_full_name(payload),
            number=payload["pull_request"]["number"],
            reviewer=_user_login(review.get("user")) or "",
            state=review["state"].upper(),
            commit_id=review["commit_id"],
        )

    if event == "check_suite" and action == "completed":
        suite = payload["check_suite"]
        return CheckSuiteCompleted(
            repo=_repo_full_name(payload),
            sha=suite["head_sha"],
            conclusion=suite.get("conclusion"),
        )

    if event == "push":
        ref = payload.get("ref", "")
        default_branch = payload.get("repository", {}).get("default_branch")
        branch = ref.removeprefix("refs/heads/")
        if default_branch and branch == default_branch:
            return PushToDefault(
                repo=_repo_full_name(payload),
                branch=branch,
                after=payload["after"],
                pusher=payload.get("pusher", {}).get("name"),
            )
        logger.debug("ignoring push to %s (default branch is %s)", ref, default_branch)
        return None

    if event == "issue_comment" and action == "created":
        comment = payload["comment"]
        return IssueCommentCreated(
            repo=_repo_full_name(payload),
            number=payload["issue"]["number"],
            body=comment.get("body") or "",
            author=_user_login(comment.get("user")),
        )

    logger.debug("ignoring webhook event=%s action=%s", event, action)
    return None
