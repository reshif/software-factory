"""LiteLLM-backed per-mission budget gateway (final draft §12.2 M6, §13.1 #7).

Every mission gets its own LiteLLM virtual key with a hard `max_budget`, so a
runaway agent loop is stopped by the gateway itself rather than by anything
this process tracks in memory. Keys are secrets: this module never logs one,
not even at debug level (red team R-A4).

LiteLLM's documented spend lookup is `GET /key/info?key=<key>`, so that one
call must carry the key in the query string. To keep it out of *our* logs,
the `httpx`/`httpcore` loggers (which log full request URLs at INFO) are
capped at WARNING, and HTTP errors are re-raised as `LiteLLMGatewayError`
with only method, path and status (no scheme, host or query) and any key
redacted. Run the gateway on a private network and don't enable
request-URL access logging on it or on any proxy in front of it.
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)
# httpx logs every request URL at INFO; one of ours carries a key in its query.
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


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
        return self._send("POST", path, json=json, redact=redact)

    def _send(self, method: str, path: str, *, json: dict | None = None, params: dict | None = None,
              redact: str | None = None) -> httpx.Response:
        url = f"{self._base_url}{path}"
        try:
            response = self._client.request(method, url, headers=self._headers(), json=json, params=params)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise self._sanitize(exc, redact) from None
        except httpx.HTTPError as exc:
            # Transport errors (connect/timeout) can also embed the full URL: strip it.
            raise LiteLLMGatewayError(f"{method} {path} -> {type(exc).__name__}") from None
        return response

    def _sanitize(self, exc: httpx.HTTPStatusError, redact: str | None) -> LiteLLMGatewayError:
        # request.url.path carries no scheme, host, or query string.
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
        # LiteLLM's documented lookup (see the module docstring for how the key is kept out of logs).
        response = self._send("GET", "/key/info", params={"key": key}, redact=key)
        return float(response.json()["info"]["spend"])

    def revoke(self, key: str) -> None:
        self._post("/key/delete", {"keys": [key]}, redact=key)
        logger.info("revoked a gateway key")

    def close(self) -> None:
        self._client.close()
