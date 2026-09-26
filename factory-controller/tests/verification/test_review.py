from factory.models import Diff, RuntimeResult
from factory.policy.action_classes import FileChange
from factory.verification.review import parse_reviewer_output, run_review

DIFF = Diff(base_commit="abc123", changes=(FileChange(path="a.py", status="modified"),), patch="diff --git a/a.py ...")


class FakeRuntime:
    def __init__(self, output_text: str, status: str = "completed", error: str | None = None,
                 raises: Exception | None = None):
        self._output_text = output_text
        self._status = status
        self._error = error
        self._raises = raises
        self.requests = []

    def run(self, request, *, on_tool_approval=None):
        self.requests.append(request)
        if self._raises:
            raise self._raises
        return RuntimeResult(session_id="s-1", status=self._status, output_text=self._output_text,
                              error=self._error)

    def resume(self, session_id, message):
        raise NotImplementedError

    def cancel(self, session_id):
        pass

    def checkpoint(self, session_id):
        return {}


def json_block(doc: str) -> str:
    return f"Some prose.\n```json\n{doc}\n```\n"


def test_pass_verdict_no_findings_is_success():
    runtime = FakeRuntime(json_block('{"verdict": "pass", "findings": []}'))
    result = run_review(runtime, DIFF, workdir="/work", model="m", max_turns=5, budget_usd=1.0)
    assert result.name == "review_agent"
    assert result.conclusion == "success"


def test_fail_verdict_is_failure():
    runtime = FakeRuntime(json_block(
        '{"verdict": "fail", "findings": [{"severity": "major", "path": "a.py", "line": 1, "message": "bad"}]}'))
    result = run_review(runtime, DIFF, workdir="/work", model="m", max_turns=5, budget_usd=1.0)
    assert result.conclusion == "failure"
    assert "a.py" in result.detail


def test_blocking_finding_fails_even_if_verdict_says_pass():
    runtime = FakeRuntime(json_block(
        '{"verdict": "pass", "findings": [{"severity": "blocking", "path": "a.py", "line": 2, "message": "sql injection"}]}'))
    result = run_review(runtime, DIFF, workdir="/work", model="m", max_turns=5, budget_usd=1.0)
    assert result.conclusion == "failure"
    assert "sql injection" in result.detail


def test_last_json_block_wins():
    text = json_block('{"verdict": "fail", "findings": []}') + "\n" + json_block('{"verdict": "pass", "findings": []}')
    runtime = FakeRuntime(text)
    result = run_review(runtime, DIFF, workdir="/work", model="m", max_turns=5, budget_usd=1.0)
    assert result.conclusion == "success"


def test_unparseable_output_is_failure():
    runtime = FakeRuntime("no json here at all")
    result = run_review(runtime, DIFF, workdir="/work", model="m", max_turns=5, budget_usd=1.0)
    assert result.conclusion == "failure"


def test_malformed_json_is_failure():
    runtime = FakeRuntime(json_block("{not valid json"))
    result = run_review(runtime, DIFF, workdir="/work", model="m", max_turns=5, budget_usd=1.0)
    assert result.conclusion == "failure"


def test_missing_findings_key_is_failure():
    runtime = FakeRuntime(json_block('{"verdict": "pass"}'))
    result = run_review(runtime, DIFF, workdir="/work", model="m", max_turns=5, budget_usd=1.0)
    assert result.conclusion == "failure"


def test_invalid_severity_is_failure():
    runtime = FakeRuntime(json_block(
        '{"verdict": "pass", "findings": [{"severity": "catastrophic", "path": "a.py", "line": 1, "message": "x"}]}'))
    result = run_review(runtime, DIFF, workdir="/work", model="m", max_turns=5, budget_usd=1.0)
    assert result.conclusion == "failure"


def test_non_completed_status_is_failure():
    runtime = FakeRuntime("", status="budget_exceeded", error="ran out of budget")
    result = run_review(runtime, DIFF, workdir="/work", model="m", max_turns=5, budget_usd=1.0)
    assert result.conclusion == "failure"
    assert "budget_exceeded" in result.detail


def test_runtime_exception_is_failure_not_raised():
    runtime = FakeRuntime("", raises=RuntimeError("sandbox exploded"))
    result = run_review(runtime, DIFF, workdir="/work", model="m", max_turns=5, budget_usd=1.0)
    assert result.conclusion == "failure"
    assert "sandbox exploded" in result.detail


def test_diff_is_passed_as_data_in_the_prompt():
    runtime = FakeRuntime(json_block('{"verdict": "pass", "findings": []}'))
    run_review(runtime, DIFF, workdir="/work", model="m", max_turns=5, budget_usd=1.0)
    request = runtime.requests[0]
    assert request.role == "reviewer"
    assert DIFF.patch in request.prompt
    assert DIFF.content_hash in request.prompt


def test_parse_reviewer_output_directly():
    assert parse_reviewer_output(json_block('{"verdict": "pass", "findings": []}')) == ("pass", [])
    assert parse_reviewer_output("nothing") is None
    assert parse_reviewer_output(json_block("[]")) is None
