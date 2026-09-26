"""Unit tests for ClaudeRuntime, entirely against a monkeypatched `query`.

No real API calls happen here: `factory.runtime.claude.query` is replaced with
a small async generator per test that yields canned SDK messages, mirroring
the real contract (final draft §12.3, build spec §3 B3).

`ClaudeRuntime` is always constructed with an explicit `cli_path` in these
tests so behavior never depends on whether `claude` happens to be on this
host's PATH.

`can_use_tool` is itself an async callback; there's no pytest-asyncio in this
project's dev dependencies (a shared file, build spec §1 rule 2), so those
tests -- and the `arun`/`aresume` tests -- drive it with a plain
`asyncio.run` instead of `@pytest.mark.asyncio`.
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

FAKE_CLI = "/usr/bin/fake-claude-cli"


@pytest.fixture
def runtime():
    rt = claude_runtime.ClaudeRuntime(cli_path=FAKE_CLI)
    yield rt
    rt.close()


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


def result_message(session_id="sess-1", subtype="success", is_error=False, **kwargs):
    return ResultMessage(subtype=subtype, duration_ms=1, duration_api_ms=1, is_error=is_error,
                          num_turns=kwargs.pop("num_turns", 1), session_id=session_id, **kwargs)


class TestGatewayKeyRequirement:
    def test_run_without_gateway_key_refuses(self, runtime):
        with pytest.raises(RuntimeError, match="gateway key"):
            runtime.run(make_request(gateway_key=None))

    def test_run_without_gateway_key_allowed_with_explicit_flag(self, monkeypatch):
        monkeypatch.setattr(claude_runtime, "query", async_messages(result_message()))
        rt = claude_runtime.ClaudeRuntime(cli_path=FAKE_CLI, allow_direct_provider_key=True)
        try:
            result = rt.run(make_request(gateway_key=None))
            assert result.status == "completed"
        finally:
            rt.close()

    def test_resume_without_gateway_key_refuses_unless_allowed(self, monkeypatch):
        monkeypatch.setattr(claude_runtime, "query", async_messages(result_message()))
        rt = claude_runtime.ClaudeRuntime(cli_path=FAKE_CLI, allow_direct_provider_key=True)
        try:
            first = rt.run(make_request(gateway_key=None))
            # flip the flag off to simulate a differently-configured resume path
            rt._allow_direct_provider_key = False
            with pytest.raises(RuntimeError, match="gateway key"):
                rt.resume(first.session_id, "continue")
        finally:
            rt.close()


class TestOptionConstruction:
    def test_builds_expected_options_and_env(self, runtime, monkeypatch):
        gen = async_messages(result_message())
        monkeypatch.setattr(claude_runtime, "query", gen)

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

    def test_cli_path_is_a_generated_wrapper_not_the_raw_target(self, runtime, monkeypatch):
        gen = async_messages(result_message())
        monkeypatch.setattr(claude_runtime, "query", gen)
        runtime.run(make_request())
        assert gen.captured_options.cli_path == runtime._wrapper_path
        assert gen.captured_options.cli_path != FAKE_CLI

    def test_env_carries_only_the_gateway_key_never_a_raw_provider_key(self, runtime, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-raw-provider-key-should-never-appear")
        gen = async_messages(result_message())
        monkeypatch.setattr(claude_runtime, "query", gen)

        request = make_request(gateway_key="sk-gateway-only", gateway_url="https://gateway.internal")
        runtime.run(request)

        env = gen.captured_options.env
        assert env["ANTHROPIC_API_KEY"] == "sk-gateway-only"
        assert env["ANTHROPIC_BASE_URL"] == "https://gateway.internal"
        assert "sk-raw-provider-key-should-never-appear" not in env.values()

    def test_home_is_a_fresh_per_run_temp_directory(self, runtime, monkeypatch):
        gen = async_messages(result_message())
        monkeypatch.setattr(claude_runtime, "query", gen)
        runtime.run(make_request())
        home = gen.captured_options.env["HOME"]
        assert home != claude_runtime.os.environ.get("HOME")
        import os
        assert os.path.isdir(home)


class TestEnvScrubbing:
    """R-A3: the agent subprocess must see none of the controller's own secrets."""

    def test_scrub_env_keeps_only_the_allowlist(self):
        poisoned = {
            "PATH": "/usr/bin", "HOME": "/home/controller", "LANG": "en_US.UTF-8",
            "ANTHROPIC_BASE_URL": "https://gateway.internal", "ANTHROPIC_API_KEY": "sk-gateway",
            "FACTORY_LLM_GATEWAY_MASTER_KEY": "sk-master-secret",
            "FACTORY_DATABASE_URL": "postgresql://user:pw@host/db",
            "GITHUB_APP_PRIVATE_KEY": "-----BEGIN PRIVATE KEY-----",
            "SOME_OTHER_SECRET": "shh",
        }
        scrubbed = claude_runtime.scrub_env(poisoned)
        assert scrubbed == {
            "PATH": "/usr/bin", "HOME": "/home/controller", "LANG": "en_US.UTF-8",
            "ANTHROPIC_BASE_URL": "https://gateway.internal", "ANTHROPIC_API_KEY": "sk-gateway",
        }

    def test_scrub_env_keeps_proxy_vars_if_present(self):
        env = {"HTTP_PROXY": "http://proxy:8888", "HTTPS_PROXY": "http://proxy:8888",
               "FACTORY_SECRET": "nope"}
        scrubbed = claude_runtime.scrub_env(env)
        assert scrubbed == {"HTTP_PROXY": "http://proxy:8888", "HTTPS_PROXY": "http://proxy:8888"}

    def test_generated_wrapper_actually_scrubs_the_environment_when_exec_d(self, tmp_path):
        """End-to-end proof, independent of the SDK: write the wrapper against a stand-in
        'real CLI' that just dumps its environment as JSON, run it with a poisoned
        environment, and check what the exec'd process actually received."""
        import json
        import subprocess
        import sys

        fake_cli = tmp_path / "fake_real_cli.py"
        fake_cli.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "json.dump(dict(os.environ), sys.stdout)\n"
        )
        fake_cli.chmod(0o700)

        wrapper_path = claude_runtime._write_env_scrubbing_wrapper(str(fake_cli))
        try:
            poisoned_env = {
                "PATH": "/usr/bin:/bin",
                "HOME": "/home/controller",
                "ANTHROPIC_API_KEY": "sk-gateway-mission-1",
                "ANTHROPIC_BASE_URL": "https://gateway.internal",
                "FACTORY_LLM_GATEWAY_MASTER_KEY": "sk-master-secret-should-not-leak",
                "FACTORY_DATABASE_URL": "postgresql://should/not/leak",
                "FACTORY_GITHUB_WEBHOOK_SECRET": "should-not-leak-either",
            }
            proc = subprocess.run(
                [sys.executable, wrapper_path, "--some-cli-flag"],
                env=poisoned_env, capture_output=True, text=True, check=True,
            )
            child_env = json.loads(proc.stdout)
        finally:
            import os
            os.remove(wrapper_path)

        assert child_env["ANTHROPIC_API_KEY"] == "sk-gateway-mission-1"
        assert child_env["ANTHROPIC_BASE_URL"] == "https://gateway.internal"
        assert "FACTORY_LLM_GATEWAY_MASTER_KEY" not in child_env
        assert "FACTORY_DATABASE_URL" not in child_env
        assert "FACTORY_GITHUB_WEBHOOK_SECRET" not in child_env
        assert not any(k.startswith("FACTORY_") for k in child_env)


class TestResultMapping:
    def test_completed_run(self, runtime, monkeypatch):
        assistant = AssistantMessage(content=[TextBlock(text='```json\n{"status": "done"}\n```')],
                                      model="claude-sonnet-5")
        msg = result_message(session_id="sess-42", total_cost_usd=1.23, num_turns=3,
                              usage={"input_tokens": 100, "output_tokens": 50})
        monkeypatch.setattr(claude_runtime, "query", async_messages(assistant, msg))

        result = runtime.run(make_request())

        assert result.status == "completed"
        assert result.session_id == "sess-42"
        assert result.usage_usd == 1.23
        assert result.num_turns == 3
        assert result.tokens == 150
        assert '```json' in result.output_text
        assert result.error is None

    @pytest.mark.parametrize("subtype", ["error_max_turns", "error_max_budget_usd"])
    def test_budget_or_turn_exhaustion_maps_to_budget_exceeded(self, runtime, monkeypatch, subtype):
        msg = result_message(session_id="sess-7", subtype=subtype, is_error=True, num_turns=10,
                              result="stopped early")
        monkeypatch.setattr(claude_runtime, "query", async_messages(msg))

        result = runtime.run(make_request())

        assert result.status == "budget_exceeded"
        assert result.session_id == "sess-7"
        assert result.error

    def test_other_errors_map_to_failed(self, runtime, monkeypatch):
        msg = result_message(session_id="sess-9", subtype="error_during_execution", is_error=True,
                              num_turns=2, result="boom")
        monkeypatch.setattr(claude_runtime, "query", async_messages(msg))

        result = runtime.run(make_request())

        assert result.status == "failed"
        assert result.error == "boom"

    def test_no_result_message_is_a_failure(self, runtime, monkeypatch):
        monkeypatch.setattr(claude_runtime, "query", async_messages())

        result = runtime.run(make_request())

        assert result.status == "failed"
        assert result.session_id == ""

    def test_result_error_exception_maps_to_failed(self, runtime, monkeypatch):
        exc = ResultError("boom", data={"subtype": "error_during_execution", "session_id": "sess-x",
                                         "errors": ["bad thing"]})
        monkeypatch.setattr(claude_runtime, "query", async_messages(raise_after=exc))

        result = runtime.run(make_request())

        assert result.status == "failed"
        assert result.session_id == "sess-x"
        assert "bad thing" in result.error

    def test_result_error_budget_subtype_maps_to_budget_exceeded(self, runtime, monkeypatch):
        exc = ResultError("boom", data={"subtype": "error_max_budget_usd", "session_id": "sess-y"})
        monkeypatch.setattr(claude_runtime, "query", async_messages(raise_after=exc))

        result = runtime.run(make_request())

        assert result.status == "budget_exceeded"
        assert result.session_id == "sess-y"


class TestCanUseTool:
    def _run_and_get_can_use_tool(self, runtime, monkeypatch, *, on_tool_approval=None, allowed_tools=("Read",)):
        gen = async_messages(result_message())
        monkeypatch.setattr(claude_runtime, "query", gen)
        runtime.run(make_request(allowed_tools=allowed_tools), on_tool_approval=on_tool_approval)
        return gen.captured_options.can_use_tool

    def test_allows_tool_already_in_allowed_tools(self, runtime, monkeypatch):
        can_use_tool = self._run_and_get_can_use_tool(runtime, monkeypatch, allowed_tools=("Read",))
        outcome = asyncio.run(can_use_tool("Read", {}, None))
        assert isinstance(outcome, PermissionResultAllow)

    def test_denies_unlisted_tool_with_no_callback(self, runtime, monkeypatch):
        can_use_tool = self._run_and_get_can_use_tool(runtime, monkeypatch, on_tool_approval=None)
        outcome = asyncio.run(can_use_tool("Bash", {"command": "rm -rf /"}, None))
        assert isinstance(outcome, PermissionResultDeny)

    def test_denies_unlisted_tool_when_callback_returns_false(self, runtime, monkeypatch):
        can_use_tool = self._run_and_get_can_use_tool(runtime, monkeypatch, on_tool_approval=lambda t, i: False)
        outcome = asyncio.run(can_use_tool("Bash", {}, None))
        assert isinstance(outcome, PermissionResultDeny)

    def test_denies_unlisted_tool_when_callback_returns_none(self, runtime, monkeypatch):
        can_use_tool = self._run_and_get_can_use_tool(runtime, monkeypatch, on_tool_approval=lambda t, i: None)
        outcome = asyncio.run(can_use_tool("Bash", {}, None))
        assert isinstance(outcome, PermissionResultDeny)

    def test_allows_unlisted_tool_when_callback_approves(self, runtime, monkeypatch):
        seen = {}

        def approve(tool, tool_input):
            seen["tool"] = tool
            seen["input"] = tool_input
            return True

        can_use_tool = self._run_and_get_can_use_tool(runtime, monkeypatch, on_tool_approval=approve)
        outcome = asyncio.run(can_use_tool("Bash", {"command": "ls"}, None))
        assert isinstance(outcome, PermissionResultAllow)
        assert seen == {"tool": "Bash", "input": {"command": "ls"}}


class TestResumeAndCheckpoint:
    def test_resume_reuses_cached_workdir_and_limits_and_sets_resume_option(self, runtime, monkeypatch):
        gen1 = async_messages(result_message(session_id="sess-1"))
        monkeypatch.setattr(claude_runtime, "query", gen1)
        request = make_request(workdir="/work/mission-9", max_turns=7, budget_usd=3.5)
        runtime.run(request)

        gen2 = async_messages(result_message(session_id="sess-1", num_turns=2))
        monkeypatch.setattr(claude_runtime, "query", gen2)
        result = runtime.resume("sess-1", "please continue")

        assert gen2.captured_options.cwd == "/work/mission-9"
        assert gen2.captured_options.resume == "sess-1"
        assert gen2.captured_options.max_turns == 7
        assert gen2.captured_options.max_budget_usd == 3.5
        assert gen2.captured_prompt == "please continue"
        assert result.num_turns == 2

    def test_resume_unknown_session_raises(self, runtime):
        with pytest.raises(RuntimeError):
            runtime.resume("no-such-session", "hi")

    def test_checkpoint_uses_get_session_messages_with_cached_workdir(self, runtime, monkeypatch):
        monkeypatch.setattr(claude_runtime, "query", async_messages(result_message(session_id="sess-5")))
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

    def test_checkpoint_never_raises_when_session_lookup_fails(self, runtime, monkeypatch):
        def boom(*args, **kwargs):
            raise RuntimeError("no transcript")

        monkeypatch.setattr(claude_runtime, "get_session_messages", boom)
        checkpoint = runtime.checkpoint("unknown-session")
        assert checkpoint == {"session_id": "unknown-session", "workdir": None, "messages": []}

    def test_cancel_drops_cached_session_so_resume_then_fails(self, runtime, monkeypatch):
        monkeypatch.setattr(claude_runtime, "query", async_messages(result_message(session_id="sess-cancel")))
        runtime.run(make_request())

        runtime.cancel("sess-cancel")

        with pytest.raises(RuntimeError):
            runtime.resume("sess-cancel", "go on")


class TestAsyncEntryPoints:
    """Q-H4: arun/aresume are the supported path from a running event loop."""

    def test_arun_completes_from_a_running_loop(self, runtime, monkeypatch):
        monkeypatch.setattr(claude_runtime, "query", async_messages(result_message(session_id="sess-async")))

        result = asyncio.run(runtime.arun(make_request()))

        assert result.status == "completed"
        assert result.session_id == "sess-async"

    def test_aresume_completes_from_a_running_loop(self, runtime, monkeypatch):
        monkeypatch.setattr(claude_runtime, "query", async_messages(result_message(session_id="sess-async-2")))
        asyncio.run(runtime.arun(make_request(max_turns=9, budget_usd=4.0)))

        monkeypatch.setattr(claude_runtime, "query",
                             async_messages(result_message(session_id="sess-async-2", num_turns=2)))

        async def go():
            return await runtime.aresume("sess-async-2", "continue")

        result = asyncio.run(go())
        assert result.status == "completed"
        assert result.num_turns == 2

    def test_sync_run_raises_inside_a_running_event_loop(self, runtime):
        async def call_sync_run_from_a_loop():
            with pytest.raises(RuntimeError, match="running event loop"):
                runtime.run(make_request())

        asyncio.run(call_sync_run_from_a_loop())

    def test_sync_resume_raises_inside_a_running_event_loop(self, runtime, monkeypatch):
        monkeypatch.setattr(claude_runtime, "query", async_messages(result_message(session_id="sess-loop")))
        runtime.run(make_request())

        async def call_sync_resume_from_a_loop():
            with pytest.raises(RuntimeError, match="running event loop"):
                runtime.resume("sess-loop", "continue")

        asyncio.run(call_sync_resume_from_a_loop())
