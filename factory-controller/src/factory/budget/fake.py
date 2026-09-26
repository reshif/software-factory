"""In-memory `BudgetGateway` for tests and `factory demo`."""
from __future__ import annotations

import uuid
from dataclasses import dataclass


class BudgetExceeded(Exception):
    """Raised by `add_spend` when spend would exceed the key's `max_usd`, like a real hard cap."""


@dataclass
class _KeyState:
    mission_id: str
    max_usd: float
    spend_usd: float = 0.0
    revoked: bool = False


class FakeBudgetGateway:
    """`BudgetGateway` with test hooks to add spend and to exceed the budget."""

    def __init__(self):
        self._keys: dict[str, _KeyState] = {}

    def create_key(self, mission_id: str, max_usd: float) -> str:
        key = f"fake-key-{uuid.uuid4().hex[:12]}"
        self._keys[key] = _KeyState(mission_id=mission_id, max_usd=max_usd)
        return key

    def spent(self, key: str) -> float:
        return self._keys[key].spend_usd

    def revoke(self, key: str) -> None:
        self._keys[key].revoked = True

    def is_revoked(self, key: str) -> bool:
        return self._keys[key].revoked

    def add_spend(self, key: str, usd: float) -> None:
        """Test/demo helper: simulate usage. Raises `BudgetExceeded` past `max_usd`."""
        state = self._keys[key]
        if state.revoked:
            raise KeyError(f"key for mission {state.mission_id} was revoked")
        state.spend_usd += usd
        if state.spend_usd > state.max_usd:
            raise BudgetExceeded(f"key for mission {state.mission_id} exceeded ${state.max_usd:.2f}")
