"""CodexRuntime is the one allowed stub in the codebase (build spec §1 rule 6):
every method raises NotImplementedError("Phase 3 exit test") until Phase 3.
"""
import pytest

from factory.models import RuntimeRequest
from factory.runtime.codex import CodexRuntime


def test_run_raises_phase3_stub_error():
    runtime = CodexRuntime()
    request = RuntimeRequest(role="implementer", prompt="go", workdir="/work", model="gpt-5-codex",
                              max_turns=5, budget_usd=1.0, allowed_tools=())
    with pytest.raises(NotImplementedError, match="Phase 3 exit test"):
        runtime.run(request)


def test_resume_raises_phase3_stub_error():
    with pytest.raises(NotImplementedError, match="Phase 3 exit test"):
        CodexRuntime().resume("session-1", "hi")


def test_cancel_raises_phase3_stub_error():
    with pytest.raises(NotImplementedError, match="Phase 3 exit test"):
        CodexRuntime().cancel("session-1")


def test_checkpoint_raises_phase3_stub_error():
    with pytest.raises(NotImplementedError, match="Phase 3 exit test"):
        CodexRuntime().checkpoint("session-1")
