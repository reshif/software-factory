"""HMAC-signed, expiring tokens for the approval inbox (final draft §10, §13.1 #8).

A token binds one approver identity and their roles to an approval request and an
expiry time. It never carries authority by itself beyond what the HMAC signature
proves: verification uses a constant-time compare and rejects both tampering and
staleness. Tokens are opaque strings passed in a query parameter (GET) or a hidden
form field (POST) — never in a header or cookie that a browser would replay
cross-site, and never evaluated as code.

A token with `request_id` set is scoped to exactly that approval request (used for
one-click Slack action links). A token with `request_id` left as `None` is a general
inbox token: valid for browsing any open request, but — the router enforces this,
not this module — never for recording a decision.

Every token also carries a `jti` (a random token id, unrelated to the approval
store's own fencing token): the router uses it, together with the store's event
log, to refuse a *decision* made by replaying the same token string twice. This is
defense in depth on top of the approval store's own protections (a duplicate
"approve" from the same approver is already a no-op, and a "revise"/"cancel" already
voids the round so a second `decide()` call fails); the jti check makes the token
itself single-use for the write path even before the store is consulted.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
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
    jti: str                      # random token id; unique per issued token
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

    def issue(self, *, approver: str, roles, expires: datetime, request_id: str | None = None,
             jti: str | None = None) -> str:
        """Build a signed token for `approver`, valid until `expires`.

        `jti` defaults to a fresh random id; callers only pass one explicitly in tests
        that need to control it.
        """
        payload = {
            "request_id": request_id,
            "approver": approver,
            "roles": sorted(set(roles)),
            "exp": int(expires.timestamp()),
            "jti": jti or secrets.token_urlsafe(12),
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
                jti=str(raw["jti"]),
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
