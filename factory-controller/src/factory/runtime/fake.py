"""Scripted `AgentRuntime` for tests and `factory demo` (build spec §3 B3).

A script is a plain callable `RuntimeRequest -> RuntimeResult`. It may write
files into `request.workdir` (standing in for what a real agent would edit)
and its `output_text` is expected to end with the fenced ```json block the
pipeline parses (final draft §12.3, build spec §4). `json_block` and
`scripted` are convenience helpers for building one.
"""
from __future__ import annotations

import dataclasses
import json
import os
import uuid
from collections.abc import Callable

from ..models import RuntimeRequest, RuntimeResult

Script = Callable[[RuntimeRequest], RuntimeResult]


def json_block(payload: dict) -> str:
    """Render `payload` as the fenced ```json block agent output formats end with (build spec §4)."""
    return "```json\n" + json.dumps(payload, indent=2) + "\n```"


def write_files(workdir: str, files: dict[str, str]) -> None:
    """Write `files` (relative path -> content) under `workdir`, creating directories as needed."""
    for rel_path, content in files.items():
        full_path = os.path.join(workdir, rel_path)
        directory = os.path.dirname(full_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with open(full_path, "w") as handle:
            handle.write(content)


def scripted(
    *,
    output: dict,
    files: dict[str, str] | None = None,
    status: str = "completed",
    usage_usd: float = 0.0,
    num_turns: int = 1,
    error: str | None = None,
) -> Script:
    """Build a `Script` that writes `files` (if any) and returns `output` as the final JSON block."""

    def script(request: RuntimeRequest) -> RuntimeResult:
        if files:
            write_files(request.workdir, files)
        return RuntimeResult(
            session_id="",
            status=status,
            output_text=json_block(output),
            usage_usd=usage_usd,
            num_turns=num_turns,
            error=error,
        )

    return script


class FakeRuntime:
    """`AgentRuntime` driven by scripts registered per role, one script per role at a time."""

    def __init__(self, scripts: dict[str, Script] | None = None):
        self._scripts: dict[str, Script] = dict(scripts or {})
        self._sessions: dict[str, RuntimeRequest] = {}

    def set_script(self, role: str, script: Script) -> None:
        self._scripts[role] = script

    def run(self, request: RuntimeRequest, *, on_tool_approval=None) -> RuntimeResult:
        script = self._scripts.get(request.role)
        if script is None:
            result = RuntimeResult(session_id="", status="failed",
                                    error=f"FakeRuntime has no script registered for role {request.role!r}")
        else:
            result = script(request)
        session_id = result.session_id or f"fake-session-{uuid.uuid4().hex[:12]}"
        if session_id != result.session_id:
            result = dataclasses.replace(result, session_id=session_id)
        self._sessions[session_id] = request
        return result

    def resume(self, session_id: str, message: str) -> RuntimeResult:
        request = self._sessions.get(session_id)
        if request is None:
            raise RuntimeError(f"cannot resume unknown session {session_id!r}: no cached run() context")
        resumed_request = dataclasses.replace(request, prompt=message)
        return self.run(resumed_request)

    def cancel(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def checkpoint(self, session_id: str) -> dict:
        request = self._sessions.get(session_id)
        return {
            "session_id": session_id,
            "workdir": request.workdir if request else None,
            "messages": [],
        }
