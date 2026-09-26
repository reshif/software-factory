"""Slack integration for the approval inbox (final draft §10, §6.3, §13.1 #8).

`SlackNotifier` implements `ports.Notifier`. **Separation of duties requires that a
bearer approval link is never shared** (final draft §6.3: distinct identities per
required role, no self-approval, and — the failure this module exists to avoid — no
one person satisfying a quorum by collecting links meant for others out of a shared
channel message). So:

- Each approver's signed, per-request inbox link is sent **only** as a Slack direct
  message to that approver (`chat.postMessage` with `channel=<their Slack user id>`,
  authenticated as the bot). No other message — and in particular no channel/webhook
  message — ever carries a token.
- The optional channel webhook gets an FYI only: title, gate, required approvals,
  expiry, and the **bare** `/inbox/{request_id}` URL with no `token` query parameter
  at all. It tells the channel that something needs attention; it grants nothing.
- Every packet-derived string (title, recommendation, gate, required, and the
  message's own `text` fallback) is escaped for Slack's mrkdwn parser, so an issue
  title of `<!channel>` or `<@U000|someone>` can't page a channel or mention a user,
  and stray `<`/`>`/`&` in untrusted text can't be read as Slack markup.

`verify_slack_signature` implements Slack's v0 request-signing scheme, for the
(future) interactive endpoint that will receive button clicks or slash commands. It
is provided now so a later module can wire it up without touching this one again.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from datetime import datetime

import httpx

from ..models import DecisionPacket
from ..ports import Clock
from .signing import TokenSigner

logger = logging.getLogger(__name__)

DEFAULT_SLACK_API_BASE_URL = "https://slack.com/api"


class InvalidSlackSignature(Exception):
    """The request signature didn't match, or the timestamp fell outside the replay window."""


class SlackApiError(Exception):
    """Slack's Web API returned `ok: false`."""


@dataclass(frozen=True)
class ApproverContact:
    """One human eligible to act on approvals this notifier sends to Slack.

    `approver` is the identity string passed to `ApprovalStore.decide` (e.g. a GitHub
    login); `slack_user_id` is the Slack member id (`U0123...`) `chat.postMessage`
    sends the DM to. `roles` are baked into that approver's signed token, so the
    security-role and quorum checks in `factory.controller.approvals` see the same
    roles the approver would present if they typed them in by hand.
    """
    approver: str
    slack_user_id: str
    roles: tuple = ()
    display_name: str | None = None

    @property
    def label(self) -> str:
        return self.display_name or self.approver


def _slack_escape(text: object) -> str:
    """Slack mrkdwn only needs &, <, > escaped (its own escaping rule, not HTML's).

    This also neutralizes control sequences that only mean something inside `<...>`:
    `<!channel>`, `<!here>`, `<@U0123>`, `<#C0123|name>` all become inert text once
    their angle brackets are entities, so untrusted packet text (an issue title, a
    summary) can never page a channel or mention someone.
    """
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SlackNotifier:
    """Posts decision-packet notifications and info messages to Slack.

    Owns an `httpx.Client` unless one is injected, in which case the caller keeps
    ownership and `close()`/the context manager are no-ops for it.
    """

    def __init__(self, *, bot_token: str, signer: TokenSigner, clock: Clock,
                roster: list[ApproverContact], webhook_url: str | None = None,
                api_base_url: str = DEFAULT_SLACK_API_BASE_URL,
                http_client: httpx.Client | None = None):
        self._bot_token = bot_token
        self._signer = signer
        self._clock = clock
        self._roster = list(roster)
        self._webhook_url = webhook_url
        self._api_base_url = api_base_url.rstrip("/")
        self._http = http_client if http_client is not None else httpx.Client(timeout=10.0)
        self._owns_http = http_client is None

    def close(self) -> None:
        """Close the underlying HTTP client — but only if this notifier created it."""
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> "SlackNotifier":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ── Notifier port ────────────────────────────────────────────────────────────
    def decision_requested(self, packet: DecisionPacket, *, inbox_url: str) -> None:
        base = inbox_url.rstrip("/")
        if self._webhook_url:
            self._post_channel_fyi(packet, base)
        for contact in self._roster:
            self._dm_approver(contact, packet, base)

    def info(self, text: str) -> None:
        if not self._webhook_url:
            logger.info("slack info (no webhook configured): %s", text)
            return
        response = self._http.post(self._webhook_url, json={"text": _slack_escape(text)})
        response.raise_for_status()

    # ── internals ────────────────────────────────────────────────────────────────
    def _post_channel_fyi(self, packet: DecisionPacket, base: str) -> None:
        """Channel-wide notice: no token, no per-approver link — just the bare packet URL."""
        plain_url = f"{base}/inbox/{packet.request_id}"
        text = (f"Approval needed: {_slack_escape(packet.title)}\n"
                f"Gate {_slack_escape(packet.gate)} · required {_slack_escape(packet.required)} · "
                f"expires {packet.expires.isoformat()}\n"
                f"Open (sign in for your own link): {plain_url}")
        blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]
        response = self._http.post(self._webhook_url, json={"text": text, "blocks": blocks})
        response.raise_for_status()

    def _dm_approver(self, contact: ApproverContact, packet: DecisionPacket, base: str) -> None:
        """Send exactly one approver exactly one signed link, as a direct message."""
        token = self._signer.issue(approver=contact.approver, roles=contact.roles, expires=packet.expires,
                                   request_id=packet.request_id)
        link = f"{base}/inbox/{packet.request_id}?token={token}"
        text = (f"Approval needed: {_slack_escape(packet.title)}\n"
                f"{_slack_escape(packet.recommendation)}")
        blocks = [
            {"type": "section", "text": {"type": "mrkdwn", "text": text}},
            {"type": "actions", "elements": [{
                "type": "button",
                "text": {"type": "plain_text", "text": "Open your approval"},
                "url": link,
                "action_id": "open_inbox",
            }]},
        ]
        response = self._http.post(
            f"{self._api_base_url}/chat.postMessage",
            headers={"Authorization": f"Bearer {self._bot_token}"},
            json={"channel": contact.slack_user_id, "text": text, "blocks": blocks},
        )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("ok", False):
            raise SlackApiError(f"chat.postMessage to {contact.approver} failed: {payload.get('error')}")


def verify_slack_signature(signing_secret: str, *, timestamp: str, body: bytes | str, signature: str,
                           now: datetime, tolerance_s: int = 300) -> None:
    """Verify a Slack v0-signed request and reject replays (5-minute window by default).

    Raises `InvalidSlackSignature` for a missing/non-numeric timestamp, a timestamp
    outside `tolerance_s` of `now`, or a signature mismatch. The comparison is
    constant-time.
    """
    try:
        sent_at = int(timestamp)
    except (TypeError, ValueError) as exc:
        raise InvalidSlackSignature("missing or non-numeric timestamp") from exc
    if abs(now.timestamp() - sent_at) > tolerance_s:
        raise InvalidSlackSignature("timestamp outside the replay window")
    if isinstance(body, str):
        body = body.encode("utf-8")
    basestring = b"v0:" + str(sent_at).encode("ascii") + b":" + body
    digest = hmac.new(signing_secret.encode("utf-8"), basestring, hashlib.sha256).hexdigest()
    expected = f"v0={digest}"
    if not hmac.compare_digest(expected, signature or ""):
        raise InvalidSlackSignature("signature mismatch")


__all__ = ["DEFAULT_SLACK_API_BASE_URL", "ApproverContact", "InvalidSlackSignature", "SlackApiError",
          "SlackNotifier", "verify_slack_signature"]
