"""In-memory item store: the whole "business logic" of the sample service.

Kept separate from server.py so it can be unit-tested without opening a
socket (final draft §9.2 layer 2: unit tests, independent of the HTTP layer).
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field


class NotFound(KeyError):
    """Raised when an item id doesn't exist."""


@dataclass
class Store:
    _items: dict = field(default_factory=dict)
    _ids: "itertools.count" = field(default_factory=lambda: itertools.count(1))

    def list(self) -> list[dict]:
        return [{"id": item_id, **body} for item_id, body in sorted(self._items.items())]

    def get(self, item_id: int) -> dict:
        try:
            return {"id": item_id, **self._items[item_id]}
        except KeyError:
            raise NotFound(item_id) from None

    def add(self, name: str) -> dict:
        if not name or not name.strip():
            raise ValueError("name is required")
        item_id = next(self._ids)
        self._items[item_id] = {"name": name}
        return {"id": item_id, "name": name}

    def delete(self, item_id: int) -> None:
        try:
            del self._items[item_id]
        except KeyError:
            raise NotFound(item_id) from None
