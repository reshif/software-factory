"""Feature flags for the flag-ramp release model (final draft §7).

"Each mission merges behind its own default-off flag, so unreleased code on main is
dormant." Flags are always created OFF (0% rollout); `kill()` is the fail-closed
escape hatch used by recovery plans and sets the rollout back to 0.
"""
import json
from pathlib import Path


class FileFlagProvider:
    """A JSON-file-backed flag store for local/single-node deployments."""

    def __init__(self, path: str):
        self._path = Path(path)

    def _load(self) -> dict[str, int]:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text())
        except json.JSONDecodeError:
            return {}

    def _save(self, state: dict[str, int]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(state))

    def create(self, flag: str) -> None:
        state = self._load()
        if flag not in state:
            state[flag] = 0
            self._save(state)

    def set_rollout(self, flag: str, percent: int) -> None:
        if not 0 <= percent <= 100:
            raise ValueError(f"rollout percent must be 0-100, got {percent}")
        state = self._load()
        state[flag] = percent
        self._save(state)

    def kill(self, flag: str) -> None:
        state = self._load()
        state[flag] = 0
        self._save(state)

    def rollout(self, flag: str) -> int:
        # Fail closed: an unknown flag has never been created, so treat it as 0% (off).
        return self._load().get(flag, 0)


class FakeFlagProvider:
    """In-memory flag provider for tests and `factory demo`, with the same semantics."""

    def __init__(self):
        self._state: dict[str, int] = {}

    def create(self, flag: str) -> None:
        self._state.setdefault(flag, 0)

    def set_rollout(self, flag: str, percent: int) -> None:
        if not 0 <= percent <= 100:
            raise ValueError(f"rollout percent must be 0-100, got {percent}")
        self._state[flag] = percent

    def kill(self, flag: str) -> None:
        self._state[flag] = 0

    def rollout(self, flag: str) -> int:
        return self._state.get(flag, 0)
