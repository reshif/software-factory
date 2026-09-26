"""Slack notifier and request-signature verification (final draft §6.3, §10, §13.1 #8).

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
from factory.inbox.slack import (DEFAULT_SLACK_API_BASE_URL, ApproverContact, InvalidSlackSignature,
                                 SlackApiError, SlackNotifier, verify_slack_signature)
from factory.store.memory import MemoryStateStore

from .conftest import make_approval, make_packet

WEBHOOK_URL = "https://hooks.slack.example/services/T000/B000/xxx"
CHAT_POST_MESSAGE = f"{DEFAULT_SLACK_API_BASE_URL}/chat.postMessage"


@pytest.fixture
def clock(now):
    return FakeClock(now)


@pytest.fixture
def signer():
    return TokenSigner("slack-test-secret")


@pytest.fixture
def roster():
    return [
        ApproverContact(approver="@tl", slack_user_id="U_TL", roles=("tech_lead",), display_name="Tess Lead"),
        ApproverContact(approver="@sec", slack_user_id="U_SEC", roles=("security",), display_name="Sam Security"),
    ]


@pytest.fixture
def notifier(signer, clock, roster):
    notifier = SlackNotifier(bot_token="xoxb-test", signer=signer, clock=clock, roster=roster,
                             webhook_url=WEBHOOK_URL)
    yield notifier
    notifier.close()


def _packet_for(now, *, title="Deploy widget v2", request_id="REQ-42"):
    store = MemoryStateStore()
    request = make_approval(store, now, request_id=request_id, required="2+sec")
    return make_packet(request, title=title)


@respx.mock
def test_channel_fyi_carries_no_token(notifier, now):
    respx.post(CHAT_POST_MESSAGE).mock(return_value=httpx.Response(200, json={"ok": True}))
    channel_route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200, json={"ok": True}))

    packet = _packet_for(now)
    notifier.decision_requested(packet, inbox_url="https://factory.example.com")

    assert channel_route.call_count == 1
    dumped = channel_route.calls[0].request.content.decode()
    assert "token=" not in dumped
    assert "/inbox/REQ-42" in dumped
    assert "?token" not in dumped


@respx.mock
def test_each_dm_carries_exactly_one_token_to_its_own_approver(notifier, signer, clock, now, roster):
    respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    dm_route = respx.post(CHAT_POST_MESSAGE).mock(
        return_value=httpx.Response(200, json={"ok": True, "ts": "123.456"}))

    packet = _packet_for(now)
    notifier.decision_requested(packet, inbox_url="https://factory.example.com")

    assert dm_route.call_count == len(roster)
    seen_channels = []
    tokens = []
    for call in dm_route.calls:
        assert call.request.headers["authorization"] == "Bearer xoxb-test"
        body = json.loads(call.request.content)
        seen_channels.append(body["channel"])
        dumped = json.dumps(body)
        assert dumped.count("token=") == 1  # exactly one link, exactly one token
        link = body["blocks"][1]["elements"][0]["url"]
        tokens.append(link.rsplit("token=", 1)[1])

    assert sorted(seen_channels) == sorted(c.slack_user_id for c in roster)
    assert len(set(tokens)) == len(roster)  # every approver got a distinct token
    for token, contact in zip(tokens, roster):
        payload = signer.verify(token, now=clock.now())
        assert payload.approver == contact.approver
        assert payload.roles == tuple(sorted(contact.roles))
        assert payload.request_id == "REQ-42"


@respx.mock
def test_channel_mention_in_title_is_neutralized(notifier, now):
    dm_route = respx.post(CHAT_POST_MESSAGE).mock(return_value=httpx.Response(200, json={"ok": True}))
    channel_route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200, json={"ok": True}))

    packet = _packet_for(now, title="<!channel> ship now")
    notifier.decision_requested(packet, inbox_url="https://factory.example.com")

    channel_dumped = channel_route.calls[0].request.content.decode()
    assert "<!channel>" not in channel_dumped
    assert "&lt;!channel&gt;" in channel_dumped

    for call in dm_route.calls:
        dm_dumped = call.request.content.decode()
        assert "<!channel>" not in dm_dumped
        assert "&lt;!channel&gt;" in dm_dumped


@respx.mock
def test_ampersand_and_angle_brackets_are_escaped_everywhere(notifier, now):
    respx.post(CHAT_POST_MESSAGE).mock(return_value=httpx.Response(200, json={"ok": True}))
    channel_route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200, json={"ok": True}))

    packet = _packet_for(now, title="Fix A & B <script>")
    notifier.decision_requested(packet, inbox_url="https://factory.example.com")

    dumped = channel_route.calls[0].request.content.decode()
    assert "A & B <script>" not in dumped
    assert "A &amp; B &lt;script&gt;" in dumped


@respx.mock
def test_decision_requested_without_webhook_only_sends_dms(signer, clock, roster, now):
    notifier = SlackNotifier(bot_token="xoxb-test", signer=signer, clock=clock, roster=roster)
    dm_route = respx.post(CHAT_POST_MESSAGE).mock(return_value=httpx.Response(200, json={"ok": True}))

    packet = _packet_for(now)
    notifier.decision_requested(packet, inbox_url="https://factory.example.com")

    assert dm_route.call_count == len(roster)
    notifier.close()


@respx.mock
def test_slack_api_error_is_raised(notifier, now):
    respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200, json={"ok": True}))
    respx.post(CHAT_POST_MESSAGE).mock(
        return_value=httpx.Response(200, json={"ok": False, "error": "channel_not_found"}))

    with pytest.raises(SlackApiError):
        notifier.decision_requested(_packet_for(now), inbox_url="https://factory.example.com")


@respx.mock
def test_info_posts_plain_text(notifier):
    route = respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200, json={"ok": True}))

    notifier.info("mission MIS-9 needs attention")

    assert route.called
    body = json.loads(route.calls.last.request.content)
    assert body == {"text": "mission MIS-9 needs attention"}


@respx.mock
def test_info_escapes_slack_markup(notifier):
    respx.post(WEBHOOK_URL).mock(return_value=httpx.Response(200, json={"ok": True}))

    notifier.info("<b>bold</b> & loud")

    body = json.loads(respx.calls.last.request.content)
    assert body["text"] == "&lt;b&gt;bold&lt;/b&gt; &amp; loud"


def test_info_without_webhook_does_not_raise(signer, clock, roster):
    notifier = SlackNotifier(bot_token="xoxb-test", signer=signer, clock=clock, roster=roster)
    notifier.info("no channel configured, this should just log")
    notifier.close()


def test_close_closes_an_owned_client(signer, clock, roster):
    notifier = SlackNotifier(bot_token="xoxb-test", signer=signer, clock=clock, roster=roster)
    assert not notifier._http.is_closed
    notifier.close()
    assert notifier._http.is_closed


def test_close_never_closes_an_injected_client(signer, clock, roster):
    injected = httpx.Client()
    notifier = SlackNotifier(bot_token="xoxb-test", signer=signer, clock=clock, roster=roster,
                             http_client=injected)
    notifier.close()
    assert not injected.is_closed
    injected.close()


def test_context_manager_closes_an_owned_client(signer, clock, roster):
    with SlackNotifier(bot_token="xoxb-test", signer=signer, clock=clock, roster=roster) as notifier:
        assert not notifier._http.is_closed
    assert notifier._http.is_closed


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
