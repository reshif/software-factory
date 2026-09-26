"""Test/demo `Notifier` adapter (final draft §12.2: every port gets a real and a fake)."""
from __future__ import annotations

from dataclasses import dataclass

from ..models import DecisionPacket


@dataclass
class Recorded:
    packet: DecisionPacket
    inbox_url: str


class RecordingNotifier:
    """`Notifier` that just remembers what it was told, for assertions in tests and the demo."""

    def __init__(self) -> None:
        self.decisions: list[Recorded] = []
        self.info_messages: list[str] = []

    def decision_requested(self, packet: DecisionPacket, *, inbox_url: str) -> None:
        self.decisions.append(Recorded(packet=packet, inbox_url=inbox_url))

    def info(self, text: str) -> None:
        self.info_messages.append(text)


__all__ = ["Recorded", "RecordingNotifier"]
