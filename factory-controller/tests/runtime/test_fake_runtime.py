"""Tests for FakeRuntime: scripted per role, used by `factory demo` and every
other module's tests instead of the real Claude Agent SDK (build spec §3 B3).
"""
import json
import os

import pytest

from factory.models import RuntimeRequest
from factory.runtime.fake import FakeRuntime, json_block, scripted


def make_request(role: str, workdir: str, **overrides) -> RuntimeRequest:
    defaults = dict(
        role=role,
        prompt="go",
        workdir=workdir,
        model="claude-sonnet-5",
        max_turns=5,
        budget_usd=2.0,
        allowed_tools=("Read", "Edit"),
    )
    defaults.update(overrides)
    return RuntimeRequest(**defaults)


def test_json_block_is_fenced_and_parseable():
    block = json_block({"status": "done", "summary": "ok"})
    assert block.startswith("```json\n")
    assert block.endswith("\n```")
    inner = block.removeprefix("```json\n").removesuffix("\n```")
    assert json.loads(inner) == {"status": "done", "summary": "ok"}


def test_run_with_no_script_registered_fails_closed(tmp_path):
    runtime = FakeRuntime()
    result = runtime.run(make_request("architect", str(tmp_path)))
    assert result.status == "failed"
    assert "architect" in result.error


def test_scripted_writes_files_and_emits_output(tmp_path):
    output = {"status": "done", "summary": "added a file", "tests_added": [], "commands_run": [], "blocker": None}
    runtime = FakeRuntime({"implementer": scripted(files={"src/x.py": "print('hi')\n"}, output=output)})

    result = runtime.run(make_request("implementer", str(tmp_path)))

    assert result.status == "completed"
    assert (tmp_path / "src" / "x.py").read_text() == "print('hi')\n"
    assert json.loads(result.output_text.removeprefix("```json\n").removesuffix("\n```")) == output


def test_intake_architect_qa_reviewer_output_shapes_round_trip(tmp_path):
    """Exercise the §4 shapes for every role FakeRuntime needs to emit."""
    outputs = {
        "intake": {"lane": "patch", "suggested_class": "AC2", "summary": "fix typo", "risk_notes": []},
        "architect": {
            "summary": "add rate limiting", "recommendation": "APPROVE: low risk",
            "acceptance_criteria": ["WHEN a client exceeds the limit THE SYSTEM SHALL return 429"],
            "tasks": [{"task_id": "T-1", "objective": "add middleware", "owned_paths": ["src/x/**"],
                       "action_class": "AC4", "acceptance_checks": ["pytest tests/test_x.py"], "depends_on": []}],
            "alternatives": [], "risks": [], "recovery_plan": "revert the PR", "estimate_usd": 12.5,
        },
        "qa": {"status": "pass", "scenarios": [{"name": "happy path", "result": "pass", "detail": ""}]},
        "reviewer": {"verdict": "pass", "findings": []},
    }
    scripts = {role: scripted(output=payload) for role, payload in outputs.items()}
    runtime = FakeRuntime(scripts)

    for role, payload in outputs.items():
        result = runtime.run(make_request(role, str(tmp_path)))
        assert result.status == "completed"
        parsed = json.loads(result.output_text.removeprefix("```json\n").removesuffix("\n```"))
        assert parsed == payload


def test_set_script_registers_after_construction(tmp_path):
    runtime = FakeRuntime()
    runtime.set_script("qa", scripted(output={"status": "pass", "scenarios": []}))
    result = runtime.run(make_request("qa", str(tmp_path)))
    assert result.status == "completed"


def test_run_assigns_a_session_id_and_resume_reuses_the_script(tmp_path):
    calls = []

    def script(request: RuntimeRequest):
        calls.append(request.prompt)
        from factory.models import RuntimeResult
        return RuntimeResult(session_id="", status="completed", output_text=json_block({"status": "done"}))

    runtime = FakeRuntime({"implementer": script})
    first = runtime.run(make_request("implementer", str(tmp_path), prompt="start"))
    assert first.session_id

    second = runtime.resume(first.session_id, "continue please")
    assert second.status == "completed"
    assert calls == ["start", "continue please"]


def test_resume_unknown_session_raises(tmp_path):
    runtime = FakeRuntime()
    with pytest.raises(RuntimeError):
        runtime.resume("no-such-session", "hi")


def test_cancel_drops_session_so_resume_then_fails(tmp_path):
    runtime = FakeRuntime({"implementer": scripted(output={"status": "done"})})
    result = runtime.run(make_request("implementer", str(tmp_path)))
    runtime.cancel(result.session_id)
    with pytest.raises(RuntimeError):
        runtime.resume(result.session_id, "continue")


def test_checkpoint_returns_workdir_for_known_session(tmp_path):
    runtime = FakeRuntime({"implementer": scripted(output={"status": "done"})})
    result = runtime.run(make_request("implementer", str(tmp_path)))
    checkpoint = runtime.checkpoint(result.session_id)
    assert checkpoint["workdir"] == str(tmp_path)
    assert checkpoint["session_id"] == result.session_id
