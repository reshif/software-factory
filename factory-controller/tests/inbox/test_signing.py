"""Unit tests for the inbox's signed tokens (final draft §13.1 #8)."""
from datetime import timedelta

import pytest

from factory.inbox.signing import InvalidToken, TokenExpired, TokenSigner


def test_round_trip(now):
    signer = TokenSigner("secret")
    token = signer.issue(approver="@tl", roles=("tech_lead", "product"), expires=now + timedelta(hours=1),
                         request_id="REQ-1")

    payload = signer.verify(token, now=now)

    assert payload.approver == "@tl"
    assert payload.roles == ("product", "tech_lead")  # sorted at issuance
    assert payload.request_id == "REQ-1"


def test_general_token_has_no_request_id(now):
    signer = TokenSigner("secret")
    token = signer.issue(approver="@tl", roles=(), expires=now + timedelta(hours=1))

    payload = signer.verify(token, now=now)

    assert payload.request_id is None


def test_tampered_signature_is_rejected(now):
    signer = TokenSigner("secret")
    token = signer.issue(approver="@tl", roles=(), expires=now + timedelta(hours=1))
    body, signature = token.rsplit(".", 1)
    flipped = "a" if signature[-1] != "a" else "b"
    tampered = f"{body}.{signature[:-1]}{flipped}"

    with pytest.raises(InvalidToken):
        signer.verify(tampered, now=now)


def test_tampered_payload_is_rejected(now):
    signer = TokenSigner("secret")
    token = signer.issue(approver="@tl", roles=(), expires=now + timedelta(hours=1),
                         request_id="REQ-1")
    body, signature = token.rsplit(".", 1)
    other_token = signer.issue(approver="@evil", roles=("security",), expires=now + timedelta(hours=1),
                               request_id="REQ-1")
    other_body, _ = other_token.rsplit(".", 1)
    # Swap in someone else's payload but keep this token's signature: must not verify.
    with pytest.raises(InvalidToken):
        signer.verify(f"{other_body}.{signature}", now=now)
    del body


def test_wrong_secret_is_rejected(now):
    token = TokenSigner("secret-a").issue(approver="@tl", roles=(), expires=now + timedelta(hours=1))

    with pytest.raises(InvalidToken):
        TokenSigner("secret-b").verify(token, now=now)


def test_expired_token_is_rejected(now):
    signer = TokenSigner("secret")
    token = signer.issue(approver="@tl", roles=(), expires=now + timedelta(minutes=5))

    with pytest.raises(TokenExpired):
        signer.verify(token, now=now + timedelta(minutes=6))


def test_expiry_is_exclusive_at_the_boundary(now):
    signer = TokenSigner("secret")
    expires = now + timedelta(minutes=5)
    token = signer.issue(approver="@tl", roles=(), expires=expires)

    with pytest.raises(TokenExpired):
        signer.verify(token, now=expires)


def test_malformed_token_is_rejected(now):
    signer = TokenSigner("secret")

    with pytest.raises(InvalidToken):
        signer.verify("not-a-token", now=now)


def test_empty_secret_is_rejected():
    with pytest.raises(ValueError):
        TokenSigner("")
