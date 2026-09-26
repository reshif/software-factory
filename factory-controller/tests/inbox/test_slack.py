"""Slack notifier and request-signature verification (final draft §10, §13.1 #8).

HTTP is mocked with respx: these tests never touch the real network or Slack.
"""
import hashlib
import hmac
import json
from datetime import timedelta

import httpx
import pytest
import respx

from factory.clock import FakeClock
from factory.inbox.signing import TokenSigner
from factory.inbox.slack import ApproverContact, InvalidSlackSignature, SlackNotifier, verify_slack_signature

from .conftest import make_approval, make_packet
from .stub_store import StubStateStore

WEBHOOK_URL = "https://hooks.slack.example/services/T000/B000/xxx"


@pytest.fixture
def clock(now):
    return FakeClock(now)


@pytest.fixture
def signer():
    return TokenSigner("slack-test-secret")


@pytest.fixture
def notifier(signer, clock):
    roster = [
        ApproverContact(approver="@tl", roles=("tech_lead",), display_name="Tess Lead"),
        ApproverContact(approver="@sec", roles=("security",), display_name="Sam Security"),
    ]
    return SlackNotifier(WEBHOOK_URL, signer=signer, clock=clock, roster=roster)


@respx.mock
def test_decision_requested_posts_block_kit_with_per_approver_links(notifier, signer, clock, now):
    route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200, json={"ok": True}))

    store = StubStateStore()
    request = make_approval(store, now, request_id="REQ-42")
    packet = make_packet(request, title="Deploy widget v2")

    notifier.decision_requested(packet, inbox_url="https://factory.example.com")

    assert route.called
    body = json.loads(route.calls.last.request.content)

    assert body["text"] == "Deploy widget v2"
    assert isinstance(body["blocks"], list)
    button_blocks = [b for b in body["blocks"] if b["type"] == "actions"]
    assert len(button_blocks) == 2  # one per roster entry

    urls = [b["elements"][0]["url"] for b in button_blocks]
    assert all(url.startswith("https://factory.example.com/inbox/REQ-42?token=") for url in urls)
    assert len(set(urls)) == 2  # each approver gets a distinct, per-approver token

    for url, contact in zip(urls, notifier._roster):
        token = url.rsplit("token=", 1)[1]
        payload = signer.verify(token, now=clock.now())
        assert payload.approver == contact.approver
        assert payload.roles == tuple(sorted(contact.roles))
        assert payload.request_id == "REQ-42"


@respx.mock
def test_info_posts_plain_text(notifier):
    route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200, json={"ok": True}))

    notifier.info("mission MIS-9 needs attention")

    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body == {"text": "mission MIS-9 needs attention"}


@respx.mock
def test_info_escapes_slack_markup(notifier):
    route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200, json={"ok": True}))

    notifier.info("<b>bold</b> & loud")

    body = json.loads(route.calls.last.request.content)
    assert body["text"] == "&lt;b&gt;bold&lt;/b&gt; &amp; loud"


def _sign(secret: str, timestamp: str, body: bytes) -> str:
    basestring = b"v0:" + timestamp.encode("ascii") + b":" + body
    digest = hmac.new(secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    return f"v0={digest}"


def test_verify_slack_signature_accepts_a_valid_request(now):
    secret = "shhh"
    body = b"payload=hello"
    timestamp = str(int(now.timestamp()))
    signature = _sign(secret, timestamp, body)

    verify_slack_signature(secret, timestamp=timestamp, body=body, signature=signature, now=now)


def test_verify_slack_signature_rejects_tampering(now):
    secret = "shhh"
    body = b"payload=hello"
    timestamp = str(int(now.timestamp()))
    signature = _sign(secret, timestamp, b"payload=tampered")

    with pytest.raises(InvalidSlackSignature):
        verify_slack_signature(secret, timestamp=timestamp, body=body, signature=signature, now=now)


def test_verify_slack_signature_rejects_replays_outside_five_minutes(now):
    secret = "shhh"
    body = b"payload=hello"
    stale_timestamp = str(int((now - timedelta(minutes=6)).timestamp()))
    signature = _sign(secret, stale_timestamp, body)

    with pytest.raises(InvalidSlackSignature):
        verify_slack_signature(secret, timestamp=stale_timestamp, body=body, signature=signature, now=now)


def test_verify_slack_signature_accepts_within_five_minutes(now):
    secret = "shhh"
    body = b"payload=hello"
    timestamp = str(int((now - timedelta(minutes=4)).timestamp()))
    signature = _sign(secret, timestamp, body)

    verify_slack_signature(secret, timestamp=timestamp, body=body, signature=signature, now=now)
