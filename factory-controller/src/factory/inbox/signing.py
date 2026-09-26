"""HMAC-signed, expiring tokens for the approval inbox (final draft §10, §13.1 #8).

A token binds one approver identity and their roles to an approval request and an
expiry time. It never carries authority by itself beyond what the HMAC signature
proves: verification uses a constant-time compare and rejects both tampering and
staleness. Tokens are opaque strings passed in a query parameter (GET) or a hidden
form field (POST) — never in a header or cookie that a browser would replay
cross-site, and never evaluated as code.

A token with `request_id` set is scoped to exactly that approval request (used for
one-click Slack action links). A token with `request_id` left as `None` is a general
inbox token: valid for browsing and acting on any open request, still bound to the
approver's identity and to the token's own expiry.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime


class TokenError(Exception):
    """Base class for token issuance/verification failures."""


class InvalidToken(TokenError):
    """The token is malformed or its signature doesn't match (tampering)."""


class TokenExpired(TokenError):
    """The signature is valid but the token's `exp` has passed."""


@dataclass(frozen=True)
class TokenPayload:
    approver: str
    roles: tuple
    exp: int                      # unix seconds
    request_id: str | None = None


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode(text + padding)
    except (ValueError, UnicodeDecodeError) as exc:
        raise InvalidToken("malformed token body") from exc


class TokenSigner:
    """Issues and verifies HMAC-SHA256 signed tokens of `{request_id, approver, roles, exp}`."""

    def __init__(self, secret: bytes | str):
        if isinstance(secret, str):
            secret = secret.encode("utf-8")
        if not secret:
            raise ValueError("signing secret must not be empty")
        self._secret = secret

    def issue(self, *, approver: str, roles, expires: datetime, request_id: str | None = None) -> str:
        """Build a signed token for `approver`, valid until `expires`."""
        payload = {
            "request_id": request_id,
            "approver": approver,
            "roles": sorted(set(roles)),
            "exp": int(expires.timestamp()),
        }
        body = _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
        return f"{body}.{self._sign(body)}"

    def verify(self, token: str, *, now: datetime) -> TokenPayload:
        """Verify signature and expiry. Raises `InvalidToken` or `TokenExpired`."""
        try:
            body, signature = token.split(".", 1)
        except ValueError as exc:
            raise InvalidToken("malformed token") from exc
        expected = self._sign(body)
        # Constant-time compare: never let tampering be detectable by timing.
        if not hmac.compare_digest(expected, signature):
            raise InvalidToken("signature mismatch")
        try:
            raw = json.loads(_b64decode(body))
            payload = TokenPayload(
                approver=str(raw["approver"]),
                roles=tuple(raw["roles"]),
                exp=int(raw["exp"]),
                request_id=raw.get("request_id"),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise InvalidToken("malformed payload") from exc
        if now.timestamp() >= payload.exp:
            raise TokenExpired(f"token for approver {payload.approver!r} expired at {payload.exp}")
        return payload

    def _sign(self, body: str) -> str:
        mac = hmac.new(self._secret, body.encode("ascii"), hashlib.sha256).digest()
        return _b64encode(mac)


__all__ = ["InvalidToken", "TokenError", "TokenExpired", "TokenPayload", "TokenSigner"]
