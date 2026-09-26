"""Slack integration for the approval inbox (final draft §10, §13.1 #8).

`SlackNotifier` implements `ports.Notifier`. It posts a Block Kit message to an
incoming webhook with one signed, per-approver inbox link per notification: every
approver on the roster gets their own URL button whose token is bound to their
identity and to the packet's own expiry, so a decision made through it is always
attributable and can never be replayed past the approval window.

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


class InvalidSlackSignature(Exception):
    """The request signature didn't match, or the timestamp fell outside the replay window."""


@dataclass(frozen=True)
class ApproverContact:
    """One human eligible to act on approvals this notifier sends to Slack.

    `approver` is the identity string passed to `ApprovalStore.decide` (e.g. a GitHub
    login); `roles` are the roles baked into that approver's signed token, so the
    security-role and quorum checks in `factory.controller.approvals` see the same
    roles the approver would present if they typed them in by hand.
    """
    approver: str
    roles: tuple = ()
    display_name: str | None = None

    @property
    def label(self) -> str:
        return self.display_name or self.approver


def _slack_escape(text: str) -> str:
    """Slack mrkdwn needs only &, <, > escaped (its own escaping rule, not HTML's)."""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class SlackNotifier:
    """Posts decision-packet notifications and info messages to a Slack incoming webhook."""

    def __init__(self, webhook_url: str, *, signer: TokenSigner, clock: Clock,
                roster: list[ApproverContact], http_client: httpx.Client | None = None):
        self._webhook_url = webhook_url
        self._signer = signer
        self._clock = clock
        self._roster = list(roster)
        self._http = http_client or httpx.Client(timeout=10.0)

    def _link_for(self, contact: ApproverContact, packet: DecisionPacket, inbox_url: str) -> str:
        token = self._signer.issue(approver=contact.approver, roles=contact.roles,
                                   expires=packet.expires, request_id=packet.request_id)
        return f"{inbox_url.rstrip('/')}/inbox/{packet.request_id}?token={token}"

    def decision_requested(self, packet: DecisionPacket, *, inbox_url: str) -> None:
        blocks: list[dict] = [
            {"type": "section", "text": {
                "type": "mrkdwn",
                "text": f"*Approval needed: {_slack_escape(packet.title)}*\n{_slack_escape(packet.recommendation)}",
            }},
            {"type": "section", "fields": [
                {"type": "mrkdwn", "text": f"*Gate:*\n{_slack_escape(packet.gate)}"},
                {"type": "mrkdwn", "text": f"*Required:*\n{_slack_escape(packet.required)}"},
                {"type": "mrkdwn", "text": f"*Expires:*\n{packet.expires.isoformat()}"},
                {"type": "mrkdwn", "text": f"*Cost:*\n${packet.cost_usd:.2f}"},
            ]},
        ]
        for contact in self._roster:
            blocks.append({
                "type": "actions",
                "elements": [{
                    "type": "button",
                    "text": {"type": "plain_text", "text": f"Open for {contact.label}"},
                    "url": self._link_for(contact, packet, inbox_url),
                    "action_id": f"open_inbox_{contact.approver}",
                }],
            })
        response = self._http.post(self._webhook_url, json={"text": packet.title, "blocks": blocks})
        response.raise_for_status()

    def info(self, text: str) -> None:
        response = self._http.post(self._webhook_url, json={"text": _slack_escape(text)})
        response.raise_for_status()


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


__all__ = ["ApproverContact", "InvalidSlackSignature", "SlackNotifier", "verify_slack_signature"]
