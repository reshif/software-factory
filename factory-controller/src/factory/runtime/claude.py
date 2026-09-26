"""Claude Agent SDK adapter (final draft §12.3): the production `AgentRuntime`.

Wraps `claude_agent_sdk.query`, which is async and one-shot. This adapter is
used from synchronous controller code, so `run`/`resume` drive the SDK's
async generator to completion themselves (§ "Wrap async calls safely for
sync callers").

Tool permissions are enforced here, not just in the prompt: `can_use_tool`
allows a call only if the tool is already in the task's `allowed_tools`:
anything else goes through `on_tool_approval`, and a missing callback or a
False/None answer denies the call (final draft §12.3 "on_tool_approval").

The only credential this adapter ever hands the SDK is the per-mission
gateway key (`RuntimeRequest.gateway_key`), pointed at the gateway via
`ANTHROPIC_BASE_URL`/`ANTHROPIC_API_KEY`. It never reads or forwards a raw
provider key from the environment (build spec §3 B3, final draft §13.1 #7).
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import threading
from collections.abc import Awaitable, Callable, Coroutine
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKError,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultError,
    ResultMessage,
    TextBlock,
    get_session_messages,
    query,
)

from ..models import RuntimeRequest, RuntimeResult

logger = logging.getLogger(__name__)

# ResultMessage.subtype values that mean the run stopped because it ran out of
# turns or money, not because it failed outright.
_BUDGET_SUBTYPES = frozenset({"error_max_turns", "error_max_budget_usd"})


@dataclasses.dataclass
class _SessionContext:
    """What `resume`/`checkpoint` need to reconstruct a follow-up call, keyed by session_id."""
    workdir: str
    model: str
    allowed_tools: tuple
    gateway_key: str | None
    gateway_url: str | None
    system_prompt: str | None
    on_tool_approval: Callable[[str, dict], bool] | None


def _run_sync(coro: Coroutine[Any, Any, RuntimeResult]) -> RuntimeResult:
    """Run `coro` to completion, from either a sync or an already-async caller."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    # We're being called from inside an event loop (e.g. from async application
    # code). asyncio.run can't nest, so drive the coroutine on its own loop in
    # a helper thread instead of blocking the caller's loop incorrectly.
    outcome: dict[str, Any] = {}

    def _runner() -> None:
        try:
            outcome["value"] = asyncio.run(coro)
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller's thread below
            outcome["error"] = exc

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in outcome:
        raise outcome["error"]
    return outcome["value"]


class ClaudeRuntime:
    """`AgentRuntime` backed by the Claude Agent SDK."""

    def __init__(self):
        self._sessions: dict[str, _SessionContext] = {}

    # ── option construction ──────────────────────────────────────────────
    def _make_can_use_tool(
        self,
        allowed_tools: tuple,
        on_tool_approval: Callable[[str, dict], bool] | None,
    ) -> Callable[[str, dict, Any], Awaitable[PermissionResultAllow | PermissionResultDeny]]:
        allowed = set(allowed_tools)

        async def can_use_tool(tool_name: str, tool_input: dict, _context: Any):
            if tool_name in allowed:
                return PermissionResultAllow()
            approved = bool(on_tool_approval(tool_name, tool_input)) if on_tool_approval else False
            if approved:
                return PermissionResultAllow()
            return PermissionResultDeny(message=f"tool {tool_name!r} is not permitted for this task")

        return can_use_tool

    def _build_options(
        self,
        *,
        workdir: str,
        model: str,
        max_turns: int | None,
        budget_usd: float | None,
        allowed_tools: tuple,
        system_prompt: str | None,
        gateway_key: str | None,
        gateway_url: str | None,
        on_tool_approval: Callable[[str, dict], bool] | None,
        resume: str | None = None,
    ) -> ClaudeAgentOptions:
        env: dict[str, str] = {}
        if gateway_url:
            env["ANTHROPIC_BASE_URL"] = gateway_url
        if gateway_key:
            # The gateway key only: never a raw provider key (final draft §13.1 #7).
            env["ANTHROPIC_API_KEY"] = gateway_key
        return ClaudeAgentOptions(
            cwd=workdir,
            model=model,
            max_turns=max_turns,
            max_budget_usd=budget_usd,
            allowed_tools=list(allowed_tools),
            system_prompt=system_prompt,
            setting_sources=["project"],
            env=env,
            can_use_tool=self._make_can_use_tool(allowed_tools, on_tool_approval),
            resume=resume,
        )

    # ── running the SDK's async generator ───────────────────────────────
    async def _collect(self, prompt: str, options: ClaudeAgentOptions) -> RuntimeResult:
        output_parts: list[str] = []
        result_message: ResultMessage | None = None
        try:
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            output_parts.append(block.text)
                elif isinstance(message, ResultMessage):
                    result_message = message
        except ResultError as exc:
            status = "budget_exceeded" if exc.subtype in _BUDGET_SUBTYPES else "failed"
            return RuntimeResult(
                session_id=exc.session_id or "",
                status=status,
                output_text="\n".join(output_parts),
                error=exc.result or "; ".join(exc.errors) or str(exc),
            )
        except ClaudeSDKError as exc:
            return RuntimeResult(session_id="", status="failed", output_text="\n".join(output_parts), error=str(exc))

        return self._map_result(result_message, "\n".join(output_parts))

    def _map_result(self, result: ResultMessage | None, output_text: str) -> RuntimeResult:
        if result is None:
            return RuntimeResult(session_id="", status="failed", output_text=output_text,
                                  error="the SDK produced no result message")
        if result.subtype in _BUDGET_SUBTYPES:
            status = "budget_exceeded"
        elif result.is_error:
            status = "failed"
        else:
            status = "completed"
        usage = result.usage or {}
        tokens = sum(v for k, v in usage.items() if k.endswith("_tokens") and isinstance(v, int))
        error = None
        if status != "completed":
            error = result.result or "run did not complete successfully"
        return RuntimeResult(
            session_id=result.session_id,
            status=status,
            output_text=output_text or (result.result or ""),
            usage_usd=result.total_cost_usd or 0.0,
            tokens=tokens,
            num_turns=result.num_turns,
            error=error,
        )

    # ── AgentRuntime ─────────────────────────────────────────────────────
    def run(self, request: RuntimeRequest, *, on_tool_approval: Callable[[str, dict], bool] | None = None) -> RuntimeResult:
        options = self._build_options(
            workdir=request.workdir,
            model=request.model,
            max_turns=request.max_turns,
            budget_usd=request.budget_usd,
            allowed_tools=request.allowed_tools,
            system_prompt=request.system_prompt,
            gateway_key=request.gateway_key,
            gateway_url=request.gateway_url,
            on_tool_approval=on_tool_approval,
        )
        result = _run_sync(self._collect(request.prompt, options))
        if result.session_id:
            self._sessions[result.session_id] = _SessionContext(
                workdir=request.workdir,
                model=request.model,
                allowed_tools=request.allowed_tools,
                gateway_key=request.gateway_key,
                gateway_url=request.gateway_url,
                system_prompt=request.system_prompt,
                on_tool_approval=on_tool_approval,
            )
        return result

    def resume(self, session_id: str, message: str) -> RuntimeResult:
        ctx = self._sessions.get(session_id)
        if ctx is None:
            raise RuntimeError(f"cannot resume unknown session {session_id!r}: no cached run() context")
        options = self._build_options(
            workdir=ctx.workdir,
            model=ctx.model,
            max_turns=None,
            budget_usd=None,
            allowed_tools=ctx.allowed_tools,
            system_prompt=ctx.system_prompt,
            gateway_key=ctx.gateway_key,
            gateway_url=ctx.gateway_url,
            on_tool_approval=ctx.on_tool_approval,
            resume=session_id,
        )
        result = _run_sync(self._collect(message, options))
        if result.session_id:
            self._sessions[result.session_id] = ctx
        return result

    def cancel(self, session_id: str) -> None:
        # `query()` is one-shot and stateless: there is no in-flight run to interrupt
        # from here. Dropping the cached context is the honest thing this adapter can
        # do — it stops `resume` from continuing a session the controller gave up on.
        if self._sessions.pop(session_id, None) is not None:
            logger.info("dropped cached context for cancelled session %s", session_id)

    def checkpoint(self, session_id: str) -> dict:
        """Session state persisted OUTSIDE the sandbox (final draft §12.3)."""
        ctx = self._sessions.get(session_id)
        workdir = ctx.workdir if ctx else None
        try:
            messages = get_session_messages(session_id, directory=workdir)
        except Exception:  # noqa: BLE001 - checkpointing must never raise
            logger.warning("could not read session transcript for %s", session_id, exc_info=True)
            messages = []
        return {
            "session_id": session_id,
            "workdir": workdir,
            "messages": [dataclasses.asdict(m) for m in messages],
        }
