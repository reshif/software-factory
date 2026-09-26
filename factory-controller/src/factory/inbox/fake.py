"""Test/demo `Notifier` adapter (final draft §12.2: every port gets a real and a fake)."""
from __future__ import annotations

from dataclasses import dataclass

from ..models import DecisionPacket


@dataclass
class Recorded:
    packet: DecisionPacket
    inbox_url: str


class FakeNotifier:
    """`Notifier` that just remembers what it was told, for assertions in tests and the demo.

    Named to match the rest of the codebase's `Fake...` adapters (`FakeGitHub`,
    `FakeRuntime`, `FakeClock`, ...). `RecordingNotifier` is kept as an alias for any
    caller written against the earlier name.
    """

    def __init__(self) -> None:
        self.decisions: list[Recorded] = []
        self.info_messages: list[str] = []

    def decision_requested(self, packet: DecisionPacket, *, inbox_url: str) -> None:
        self.decisions.append(Recorded(packet=packet, inbox_url=inbox_url))

    def info(self, text: str) -> None:
        self.info_messages.append(text)


RecordingNotifier = FakeNotifier


__all__ = ["FakeNotifier", "Recorded", "RecordingNotifier"]
