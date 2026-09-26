"""`/webhooks/github` (build spec §3 B7 `app.py`): thin, durable, fail-closed.

These tests inject a duck-typed fake `Factory` (`create_app(settings,
factory=...)` is designed for exactly this) rather than the real
`pipeline.orchestrator.Factory`, so they exercise only what `app.py` itself is
responsible for: verifying the signature, requiring `X-GitHub-Delivery`,
parsing just enough to know whether to enqueue, and calling
`factory.enqueue_webhook(delivery_id, event, payload)` -- never processing a
webhook inline. `FakeFactory.enqueue_webhook` implements the same
durable/deduplicated contract `Factory.enqueue_webhook` is specified to have,
so "a duplicate delivery id is enqueued once" is a meaningful assertion here
even before that real implementation lands.
"""
from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from factory.app import create_app
from factory.config import Settings

WEBHOOK_SECRET = "test-webhook-secret"


class FakeFactory:
    """Stands in for `pipeline.orchestrator.Factory` in `app.py`-only tests."""

    def __init__(self) -> None:
        self.enqueued: list[tuple[str, str, dict]] = []
        self._seen_delivery_ids: set[str] = set()
        self.store = SimpleNamespace(list_missions=lambda: [])

    def enqueue_webhook(self, delivery_id: str, event: str, payload: dict) -> None:
        if delivery_id in self._seen_delivery_ids:
            return
        self._seen_delivery_ids.add(delivery_id)
        self.enqueued.append((delivery_id, event, payload))

    def decide(self, *args, **kwargs):  # pragma: no cover -- inbox isn't mounted in these tests
        raise NotImplementedError


@pytest.fixture
def factory() -> FakeFactory:
    return FakeFactory()


@pytest.fixture
def client(factory: FakeFactory) -> TestClient:
    settings = Settings(mode="local", github_webhook_secret=WEBHOOK_SECRET)
    app = create_app(settings, factory=factory)
    return TestClient(app)


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(WEBHOOK_SECRET.encode(), body, hashlib.sha256).hexdigest()


def _post(client: TestClient, *, event: str, payload: dict, delivery: str | None = "d-1", signed: bool = True):
    body = json.dumps(payload).encode()
    headers = {"X-GitHub-Event": event, "Content-Type": "application/json"}
    if signed:
        headers["X-Hub-Signature-256"] = _sign(body)
    if delivery is not None:
        headers["X-GitHub-Delivery"] = delivery
    return client.post("/webhooks/github", content=body, headers=headers)


def _issue_labeled_payload(label: str = "factory:feature") -> dict:
    return {
        "action": "labeled",
        "repository": {"full_name": "org/backend-service"},
        "issue": {"number": 42, "title": "Add a thing", "body": "please", "user": {"login": "dev"}},
        "label": {"name": label},
    }


# ── the happy path: verify, require a delivery id, enqueue, return fast ─────────
def test_valid_webhook_is_enqueued_and_returns_204(client, factory):
    response = _post(client, event="issues", payload=_issue_labeled_payload(), delivery="d-happy")

    assert response.status_code == 204
    assert response.content == b""
    assert len(factory.enqueued) == 1
    delivery_id, kind, payload = factory.enqueued[0]
    assert delivery_id == "d-happy"
    assert kind == "IssueLabeled"
    assert payload["label"] == "factory:feature"
    assert payload["repo"] == "org/backend-service"


# ── durability: a redelivered X-GitHub-Delivery id is enqueued once ─────────────
def test_duplicate_delivery_id_is_enqueued_once(client, factory):
    first = _post(client, event="issues", payload=_issue_labeled_payload(), delivery="d-dup")
    second = _post(client, event="issues", payload=_issue_labeled_payload(), delivery="d-dup")

    assert first.status_code == 204
    assert second.status_code == 204
    assert len(factory.enqueued) == 1, "GitHub's own webhook retries must not double-enqueue a delivery"


# ── missing X-GitHub-Delivery: 400, never enqueued ──────────────────────────────
def test_missing_delivery_header_is_400(client, factory):
    response = _post(client, event="issues", payload=_issue_labeled_payload(), delivery=None)

    assert response.status_code == 400
    assert factory.enqueued == []


# ── a signed but malformed payload: 400, never a 500 ────────────────────────────
@pytest.mark.parametrize("event,payload", [
    # pull_request_review/submitted with no "review" key -> KeyError in parse_event
    ("pull_request_review", {"action": "submitted", "repository": {"full_name": "org/x"},
                             "pull_request": {"number": 1}}),
    # check_suite/completed with no "check_suite" key -> KeyError
    ("check_suite", {"action": "completed", "repository": {"full_name": "org/x"}}),
    # issues/labeled with "issue" present but missing "number" -> KeyError
    ("issues", {"action": "labeled", "repository": {"full_name": "org/x"},
               "issue": {"title": "t", "user": {"login": "dev"}}, "label": {"name": "factory:feature"}}),
    # pull_request_review/submitted whose review.state isn't a string -> AttributeError on .upper()
    ("pull_request_review", {"action": "submitted", "repository": {"full_name": "org/x"},
                             "pull_request": {"number": 1},
                             "review": {"user": {"login": "r"}, "state": None, "commit_id": "abc"}}),
])
def test_malformed_signed_payload_is_400_not_500(client, factory, event, payload):
    response = _post(client, event=event, payload=payload, delivery="d-malformed")

    assert response.status_code == 400
    assert factory.enqueued == []


# ── an unrecognized/ignored event: 204, never enqueued, never an error ──────────
def test_unrecognized_event_is_204_and_not_enqueued(client, factory):
    response = _post(client, event="ping", payload={"zen": "hi"}, delivery="d-ping")

    assert response.status_code == 204
    assert factory.enqueued == []


# ── signature verification is unchanged ─────────────────────────────────────────
def test_invalid_signature_is_401(client, factory):
    response = _post(client, event="issues", payload=_issue_labeled_payload(), delivery="d-bad-sig", signed=False)

    assert response.status_code == 401
    assert factory.enqueued == []


# ── the handler never blocks on real work: it never touches the sandbox/GitHub ──
def test_handler_never_calls_anything_but_enqueue(client, factory, monkeypatch):
    def boom(*args, **kwargs):
        raise AssertionError("app.py must not process a webhook inline")

    monkeypatch.setattr(factory, "decide", boom)
    response = _post(client, event="issues", payload=_issue_labeled_payload(), delivery="d-fast")
    assert response.status_code == 204
