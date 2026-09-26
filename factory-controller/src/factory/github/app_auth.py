"""GitHub App authentication: JWT (RS256) -> installation access token, cached.

Final draft §11 (push bot / merge bot are separate GitHub App identities) and
§13.1 #9 (one identity per bot). Each `InstallationTokenProvider` mints tokens
for exactly one App identity; the token text is never logged, and callers are
responsible for keeping it out of anything written to disk (see
`factory.github.client` for how the git flow avoids that).
"""
import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import jwt

from ._http import github_headers

logger = logging.getLogger(__name__)

# Refresh this long before GitHub's own expiry so a slow caller never races it.
TOKEN_SAFETY_MARGIN_S = 60
# GitHub App JWTs may not be issued more than 10 minutes in the future.
JWT_TTL_S = 9 * 60
JWT_CLOCK_SKEW_S = 30


@dataclass(frozen=True)
class AppCredentials:
    """One GitHub App identity. Push bot and merge bot each get their own (never shared)."""
    app_id: str
    private_key_path: str
    installation_id: str
    api_url: str = "https://api.github.com"


def _parse_expiry_seconds(expires_at: str) -> float:
    dt = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    return (dt - datetime.now(timezone.utc)).total_seconds()


class InstallationTokenProvider:
    """Mints and caches an installation access token for one GitHub App identity."""

    def __init__(self, credentials: AppCredentials, *, client: httpx.Client | None = None,
                 monotonic=time.monotonic, wall_clock=time.time):
        self._creds = credentials
        self._owns = client is None
        self._client = client or httpx.Client(timeout=10.0)
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0

    def token(self) -> str:
        """Returns a cached token, refreshing it if it's within the safety margin of expiry."""
        with self._lock:
            if self._token is not None and self._monotonic() < self._expires_at:
                return self._token
            self._token = self._fetch()
            return self._token

    def invalidate(self) -> None:
        """Forces the next `token()` call to mint a fresh token (e.g. after a 401)."""
        with self._lock:
            self._token = None
            self._expires_at = 0.0

    def close(self) -> None:
        if self._owns:
            self._client.close()

    def __enter__(self) -> "InstallationTokenProvider":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def _private_key(self) -> str:
        return Path(self._creds.private_key_path).read_text()

    def _make_jwt(self) -> str:
        now = int(self._wall_clock())
        payload = {
            "iat": now - JWT_CLOCK_SKEW_S,
            "exp": now + JWT_TTL_S,
            "iss": self._creds.app_id,
        }
        return jwt.encode(payload, self._private_key(), algorithm="RS256")

    def _fetch(self) -> str:
        app_jwt = self._make_jwt()
        url = f"{self._creds.api_url}/app/installations/{self._creds.installation_id}/access_tokens"
        response = self._client.post(url, headers=github_headers(app_jwt))
        response.raise_for_status()
        data = response.json()
        ttl = _parse_expiry_seconds(data["expires_at"])
        self._expires_at = self._monotonic() + max(ttl - TOKEN_SAFETY_MARGIN_S, 0)
        logger.info("minted installation token for app %s, installation %s (ttl %.0fs)",
                    self._creds.app_id, self._creds.installation_id, ttl)
        return data["token"]
