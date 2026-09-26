"""Unit tests for ClaudeRuntime, entirely against a monkeypatched `query`.

No real API calls happen here: `factory.runtime.claude.query` is replaced with
a small async generator per test that yields canned SDK messages, mirroring
the real contract (final draft §12.3, build spec §3 B3).

`can_use_tool` is itself an async callback; there's no pytest-asyncio in this
project's dev dependencies (they're a shared file, build spec §1 rule 2), so
those tests drive it with a plain `asyncio.run` instead of an async test.
"""
import asyncio

import pytest
from claude_agent_sdk import (
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultError,
    ResultMessage,
    TextBlock,
)

from factory.models import RuntimeRequest
from factory.runtime import claude as claude_runtime


def make_request(**overrides) -> RuntimeRequest:
    defaults = dict(
        role="implementer",
        prompt="do the thing",
        workdir="/work/mission-1",
        model="claude-sonnet-5",
        max_turns=10,
        budget_usd=5.0,
        allowed_tools=("Read", "Edit"),
        gateway_key="sk-gateway-mission-1",
        gateway_url="https://gateway.internal",
        system_prompt="You are an implementer.",
    )
    defaults.update(overrides)
    return RuntimeRequest(**defaults)


def async_messages(*messages, raise_after: Exception | None = None):
    async def _gen(*, prompt, options):
        _gen.captured_prompt = prompt
        _gen.captured_options = options
        for message in messages:
            yield message
        if raise_after is not None:
            raise raise_after

    return _gen


class TestOptionConstruction:
    def test_builds_expected_options_and_env(self, monkeypatch):
        result_message = ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="sess-1", total_cost_usd=0.01,
        )
        gen = async_messages(result_message)
        monkeypatch.setattr(claude_runtime, "query", gen)

        runtime = claude_runtime.ClaudeRuntime()
        request = make_request()
        runtime.run(request)

        options = gen.captured_options
        assert options.cwd == request.workdir
        assert options.model == request.model
        assert options.max_turns == request.max_turns
        assert options.max_budget_usd == request.budget_usd
        assert options.allowed_tools == list(request.allowed_tools)
        assert options.system_prompt == request.system_prompt
        assert options.setting_sources == ["project"]
        assert gen.captured_prompt == request.prompt

    def test_env_carries_only_the_gateway_key_never_a_raw_provider_key(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-raw-provider-key-should-never-appear")
        result_message = ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="sess-1",
        )
        gen = async_messages(result_message)
        monkeypatch.setattr(claude_runtime, "query", gen)

        runtime = claude_runtime.ClaudeRuntime()
        request = make_request(gateway_key="sk-gateway-only", gateway_url="https://gateway.internal")
        runtime.run(request)

        env = gen.captured_options.env
        assert env["ANTHROPIC_API_KEY"] == "sk-gateway-only"
        assert env["ANTHROPIC_BASE_URL"] == "https://gateway.internal"
        assert "sk-raw-provider-key-should-never-appear" not in env.values()

    def test_missing_gateway_credentials_are_simply_omitted(self, monkeypatch):
        result_message = ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="sess-1",
        )
        gen = async_messages(result_message)
        monkeypatch.setattr(claude_runtime, "query", gen)

        runtime = claude_runtime.ClaudeRuntime()
        request = make_request(gateway_key=None, gateway_url=None)
        runtime.run(request)

        assert gen.captured_options.env == {}


class TestResultMapping:
    def test_completed_run(self, monkeypatch):
        assistant = AssistantMessage(content=[TextBlock(text='```json\n{"status": "done"}\n```')],
                                      model="claude-sonnet-5")
        result_message = ResultMessage(
            subtype="success", duration_ms=1200, duration_api_ms=1000, is_error=False,
            num_turns=3, session_id="sess-42", total_cost_usd=1.23,
            usage={"input_tokens": 100, "output_tokens": 50},
        )
        monkeypatch.setattr(claude_runtime, "query", async_messages(assistant, result_message))

        runtime = claude_runtime.ClaudeRuntime()
        result = runtime.run(make_request())

        assert result.status == "completed"
        assert result.session_id == "sess-42"
        assert result.usage_usd == 1.23
        assert result.num_turns == 3
        assert result.tokens == 150
        assert '```json' in result.output_text
        assert result.error is None

    @pytest.mark.parametrize("subtype", ["error_max_turns", "error_max_budget_usd"])
    def test_budget_or_turn_exhaustion_maps_to_budget_exceeded(self, monkeypatch, subtype):
        result_message = ResultMessage(
            subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=True,
            num_turns=10, session_id="sess-7", result="stopped early",
        )
        monkeypatch.setattr(claude_runtime, "query", async_messages(result_message))

        runtime = claude_runtime.ClaudeRuntime()
        result = runtime.run(make_request())

        assert result.status == "budget_exceeded"
        assert result.session_id == "sess-7"
        assert result.error

    def test_other_errors_map_to_failed(self, monkeypatch):
        result_message = ResultMessage(
            subtype="error_during_execution", duration_ms=1, duration_api_ms=1, is_error=True,
            num_turns=2, session_id="sess-9", result="boom",
        )
        monkeypatch.setattr(claude_runtime, "query", async_messages(result_message))

        runtime = claude_runtime.ClaudeRuntime()
        result = runtime.run(make_request())

        assert result.status == "failed"
        assert result.error == "boom"

    def test_no_result_message_is_a_failure(self, monkeypatch):
        monkeypatch.setattr(claude_runtime, "query", async_messages())

        runtime = claude_runtime.ClaudeRuntime()
        result = runtime.run(make_request())

        assert result.status == "failed"
        assert result.session_id == ""

    def test_result_error_exception_maps_to_failed(self, monkeypatch):
        exc = ResultError("boom", data={"subtype": "error_during_execution", "session_id": "sess-x",
                                         "errors": ["bad thing"]})
        monkeypatch.setattr(claude_runtime, "query", async_messages(raise_after=exc))

        runtime = claude_runtime.ClaudeRuntime()
        result = runtime.run(make_request())

        assert result.status == "failed"
        assert result.session_id == "sess-x"
        assert "bad thing" in result.error

    def test_result_error_budget_subtype_maps_to_budget_exceeded(self, monkeypatch):
        exc = ResultError("boom", data={"subtype": "error_max_budget_usd", "session_id": "sess-y"})
        monkeypatch.setattr(claude_runtime, "query", async_messages(raise_after=exc))

        runtime = claude_runtime.ClaudeRuntime()
        result = runtime.run(make_request())

        assert result.status == "budget_exceeded"
        assert result.session_id == "sess-y"


class TestCanUseTool:
    def _run_and_get_can_use_tool(self, monkeypatch, *, on_tool_approval=None, allowed_tools=("Read",)):
        result_message = ResultMessage(
            subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
            num_turns=1, session_id="sess-1",
        )
        gen = async_messages(result_message)
        monkeypatch.setattr(claude_runtime, "query", gen)
        runtime = claude_runtime.ClaudeRuntime()
        runtime.run(make_request(allowed_tools=allowed_tools), on_tool_approval=on_tool_approval)
        return gen.captured_options.can_use_tool

    def test_allows_tool_already_in_allowed_tools(self, monkeypatch):
        can_use_tool = self._run_and_get_can_use_tool(monkeypatch, allowed_tools=("Read",))
        outcome = asyncio.run(can_use_tool("Read", {}, None))
        assert isinstance(outcome, PermissionResultAllow)

    def test_denies_unlisted_tool_with_no_callback(self, monkeypatch):
        can_use_tool = self._run_and_get_can_use_tool(monkeypatch, on_tool_approval=None)
        outcome = asyncio.run(can_use_tool("Bash", {"command": "rm -rf /"}, None))
        assert isinstance(outcome, PermissionResultDeny)

    def test_denies_unlisted_tool_when_callback_returns_false(self, monkeypatch):
        can_use_tool = self._run_and_get_can_use_tool(monkeypatch, on_tool_approval=lambda t, i: False)
        outcome = asyncio.run(can_use_tool("Bash", {}, None))
        assert isinstance(outcome, PermissionResultDeny)

    def test_denies_unlisted_tool_when_callback_returns_none(self, monkeypatch):
        can_use_tool = self._run_and_get_can_use_tool(monkeypatch, on_tool_approval=lambda t, i: None)
        outcome = asyncio.run(can_use_tool("Bash", {}, None))
        assert isinstance(outcome, PermissionResultDeny)

    def test_allows_unlisted_tool_when_callback_approves(self, monkeypatch):
        seen = {}

        def approve(tool, tool_input):
            seen["tool"] = tool
            seen["input"] = tool_input
            return True

        can_use_tool = self._run_and_get_can_use_tool(monkeypatch, on_tool_approval=approve)
        outcome = asyncio.run(can_use_tool("Bash", {"command": "ls"}, None))
        assert isinstance(outcome, PermissionResultAllow)
        assert seen == {"tool": "Bash", "input": {"command": "ls"}}


class TestResumeAndCheckpoint:
    def test_resume_reuses_cached_workdir_and_sets_resume_option(self, monkeypatch):
        first_result = ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                      num_turns=1, session_id="sess-1")
        gen1 = async_messages(first_result)
        monkeypatch.setattr(claude_runtime, "query", gen1)
        runtime = claude_runtime.ClaudeRuntime()
        request = make_request(workdir="/work/mission-9")
        runtime.run(request)

        second_result = ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                       num_turns=2, session_id="sess-1")
        gen2 = async_messages(second_result)
        monkeypatch.setattr(claude_runtime, "query", gen2)
        result = runtime.resume("sess-1", "please continue")

        assert gen2.captured_options.cwd == "/work/mission-9"
        assert gen2.captured_options.resume == "sess-1"
        assert gen2.captured_prompt == "please continue"
        assert result.num_turns == 2

    def test_resume_unknown_session_raises(self):
        runtime = claude_runtime.ClaudeRuntime()
        with pytest.raises(RuntimeError):
            runtime.resume("no-such-session", "hi")

    def test_checkpoint_uses_get_session_messages_with_cached_workdir(self, monkeypatch):
        result_message = ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                        num_turns=1, session_id="sess-5")
        monkeypatch.setattr(claude_runtime, "query", async_messages(result_message))
        runtime = claude_runtime.ClaudeRuntime()
        runtime.run(make_request(workdir="/work/mission-5"))

        captured = {}

        def fake_get_session_messages(session_id, directory=None, **kwargs):
            captured["session_id"] = session_id
            captured["directory"] = directory
            return []

        monkeypatch.setattr(claude_runtime, "get_session_messages", fake_get_session_messages)
        checkpoint = runtime.checkpoint("sess-5")

        assert captured == {"session_id": "sess-5", "directory": "/work/mission-5"}
        assert checkpoint["session_id"] == "sess-5"
        assert checkpoint["workdir"] == "/work/mission-5"
        assert checkpoint["messages"] == []

    def test_checkpoint_never_raises_when_session_lookup_fails(self, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("no transcript")

        monkeypatch.setattr(claude_runtime, "get_session_messages", boom)
        runtime = claude_runtime.ClaudeRuntime()
        checkpoint = runtime.checkpoint("unknown-session")
        assert checkpoint == {"session_id": "unknown-session", "workdir": None, "messages": []}

    def test_cancel_drops_cached_session_so_resume_then_fails(self, monkeypatch):
        result_message = ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False,
                                        num_turns=1, session_id="sess-cancel")
        monkeypatch.setattr(claude_runtime, "query", async_messages(result_message))
        runtime = claude_runtime.ClaudeRuntime()
        runtime.run(make_request())

        runtime.cancel("sess-cancel")

        with pytest.raises(RuntimeError):
            runtime.resume("sess-cancel", "go on")
