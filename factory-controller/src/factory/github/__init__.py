"""GitHub adapters: App auth, REST client (push bot / merge bot), webhooks, fake, repo setup.

Final draft §11, §12.2 (M3 edges), §13.1, §13.2.
"""
from .app_auth import AppCredentials, InstallationTokenProvider
from .client import ForbiddenDiff, MergeConflict, RestGitHub, default_remote_url
from .fake import FakeGitHub
from .mirror import FakeMirror, GitMirror, mirror_path
from .setup import apply_repo_settings, build_plan, recommended_ruleset, workflow_permissions_payload
from .webhooks import (CheckSuiteCompleted, IssueCommentCreated, IssueLabeled, PullRequestReview,
                       PushToBranch, PushToDefault, SIGNATURE_HEADER, WebhookEvent, parse_event,
                       verify_signature)

__all__ = [
    "AppCredentials", "InstallationTokenProvider",
    "RestGitHub", "ForbiddenDiff", "MergeConflict", "default_remote_url",
    "FakeGitHub",
    "GitMirror", "FakeMirror", "mirror_path",
    "recommended_ruleset", "workflow_permissions_payload", "build_plan", "apply_repo_settings",
    "SIGNATURE_HEADER", "verify_signature", "parse_event", "WebhookEvent",
    "IssueLabeled", "PullRequestReview", "CheckSuiteCompleted", "PushToDefault", "PushToBranch",
    "IssueCommentCreated",
]
