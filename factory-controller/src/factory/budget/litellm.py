"""LiteLLM-backed per-mission budget gateway (final draft §12.2 M6, §13.1 #7).

Every mission gets its own LiteLLM virtual key with a hard `max_budget`, so a
runaway agent loop is stopped by the gateway itself rather than by anything
this process tracks in memory. Keys are secrets: this module never logs one,
not even at debug level, and never puts one in a URL or query string, where
it would end up in access logs and any intermediate proxy's own logging
(red team R-A4) — every call is a POST with the key in the JSON body,
alongside the master key in the `Authorization` header. HTTP errors are
re-raised as `LiteLLMGatewayError` with the scheme/host/query stripped from
the message (just the method, path and status) and any key redacted, so
neither secret can leak through an exception's own text either.
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class LiteLLMGatewayError(Exception):
    """A LiteLLM admin API call failed. Message never contains a full URL, query, or key."""


class LiteLLMGateway:
    """`BudgetGateway` backed by a LiteLLM proxy's `/key/*` admin API."""

    def __init__(self, *, base_url: str, master_key: str, client: httpx.Client | None = None, timeout: float = 30.0):
        self._base_url = base_url.rstrip("/")
        self._master_key = master_key
        self._client = client or httpx.Client(timeout=timeout)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._master_key}"}

    def _post(self, path: str, json: dict, *, redact: str | None = None) -> httpx.Response:
        url = f"{self._base_url}{path}"
        try:
            response = self._client.post(url, headers=self._headers(), json=json)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise self._sanitize(exc, redact) from None
        return response

    def _sanitize(self, exc: httpx.HTTPStatusError, redact: str | None) -> LiteLLMGatewayError:
        # request.url.path carries no scheme, host, or query string -- never a key,
        # since every call here sends the key in the body, not the query.
        message = f"{exc.request.method} {exc.request.url.path} -> {exc.response.status_code}"
        if redact:
            message = message.replace(redact, "***REDACTED***")
        return LiteLLMGatewayError(message)

    def create_key(self, mission_id: str, max_usd: float) -> str:
        response = self._post("/key/generate", {"max_budget": max_usd, "metadata": {"mission_id": mission_id}})
        key = response.json()["key"]
        logger.info("created gateway key for mission %s (max $%.2f)", mission_id, max_usd)
        return key

    def spent(self, key: str) -> float:
        # POST with the key in the body, not GET with it in the query string (R-A4):
        # a query string is what ends up verbatim in access logs and proxy logs.
        response = self._post("/key/info", {"key": key}, redact=key)
        return float(response.json()["info"]["spend"])

    def revoke(self, key: str) -> None:
        self._post("/key/delete", {"keys": [key]}, redact=key)
        logger.info("revoked a gateway key")

    def close(self) -> None:
        self._client.close()
