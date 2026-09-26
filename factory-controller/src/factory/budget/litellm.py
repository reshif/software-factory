"""LiteLLM-backed per-mission budget gateway (final draft §12.2 M6, §13.1 #7).

Every mission gets its own LiteLLM virtual key with a hard `max_budget`, so a
runaway agent loop is stopped by the gateway itself rather than by anything
this process tracks in memory. Keys are secrets: this module never logs one,
not even at debug level, and never raises them inside an exception message.
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)


class LiteLLMGateway:
    """`BudgetGateway` backed by a LiteLLM proxy's `/key/*` admin API."""

    def __init__(self, *, base_url: str, master_key: str, client: httpx.Client | None = None, timeout: float = 30.0):
        self._base_url = base_url.rstrip("/")
        self._master_key = master_key
        self._client = client or httpx.Client(timeout=timeout)

    def _headers(self) -> dict:
        return {"Authorization": f"Bearer {self._master_key}"}

    def create_key(self, mission_id: str, max_usd: float) -> str:
        response = self._client.post(
            f"{self._base_url}/key/generate",
            headers=self._headers(),
            json={"max_budget": max_usd, "metadata": {"mission_id": mission_id}},
        )
        response.raise_for_status()
        key = response.json()["key"]
        logger.info("created gateway key for mission %s (max $%.2f)", mission_id, max_usd)
        return key

    def spent(self, key: str) -> float:
        response = self._client.get(f"{self._base_url}/key/info", headers=self._headers(), params={"key": key})
        response.raise_for_status()
        return float(response.json()["info"]["spend"])

    def revoke(self, key: str) -> None:
        response = self._client.post(f"{self._base_url}/key/delete", headers=self._headers(), json={"keys": [key]})
        response.raise_for_status()
        logger.info("revoked a gateway key")

    def close(self) -> None:
        self._client.close()
