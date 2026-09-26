"""Codex adapter stub (Phase 3 exit test; final draft §12.3, §16, §19.2 Phase 3).

Codex is a second `AgentRuntime` used to prove the factory isn't locked to one
vendor: by Phase 3, one lane must run end to end on it. It has no
PreToolUse-style hooks and no in-process budget of its own, so — unlike
`ClaudeRuntime` — it cannot enforce tool permissions or spend from inside the
adapter. That enforcement has to live where it already lives for every
runtime: the sandbox (`network=False` by default), the LLM gateway (per-key
hard budgets) and the controller (gates, approvals). Implementing that mapping
is explicitly out of scope for Phase 2 (build spec §3 B3); this class is the
one allowed stub in the codebase.
"""
from __future__ import annotations

from ..models import RuntimeRequest, RuntimeResult


class CodexRuntime:
    """`AgentRuntime` placeholder for the Codex SDK / `codex exec` (Phase 3)."""

    def run(self, request: RuntimeRequest, *, on_tool_approval=None) -> RuntimeResult:
        raise NotImplementedError("Phase 3 exit test")

    def resume(self, session_id: str, message: str) -> RuntimeResult:
        raise NotImplementedError("Phase 3 exit test")

    def cancel(self, session_id: str) -> None:
        raise NotImplementedError("Phase 3 exit test")

    def checkpoint(self, session_id: str) -> dict:
        raise NotImplementedError("Phase 3 exit test")
