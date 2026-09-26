"""verification.local_checks, including the R-A7 check-gaming defense.

`src/factory/sandbox/fake.py` does not exist yet (B3 hasn't landed it as of this
merge), so most tests below use a small in-test SandboxPort fake. The gaming
scenario itself uses the real `LocalSandbox` (already on `main`) against a real
git checkout, so it's an actual end-to-end proof rather than a mocked one.
"""
from pathlib import Path

import pytest

from factory.models import ExecResult, SandboxHandle
from factory.sandbox.local import LocalSandbox
from factory.verification.local_checks import run_local_checks


class FakeSandbox:
    """Minimal SandboxPort fake: scripted exec results, records the calls made.

    `exec()` is keyed by the literal shell command string (the last element of the
    `["sh", "-c", command]` argv that `run_local_checks` builds), matching how a real
    sandbox would receive it.
    """

    def __init__(self, results: dict[str, ExecResult] | None = None, raises: dict[str, Exception] | None = None):
        self._results = results or {}
        self._raises = raises or {}
        self.calls: list[dict] = []

    def create(self, repo, base_commit):
        return SandboxHandle(sandbox_id="sbx-1", workdir="/work", base_commit=base_commit)

    def exec(self, handle, cmd, *, network=False, timeout_s=900):
        assert cmd[:2] == ["sh", "-c"], f"expected ['sh', '-c', ...], got {cmd!r}"
        command = cmd[2]
        self.calls.append({"cmd": cmd, "network": network, "timeout_s": timeout_s})
        if command in self._raises:
            raise self._raises[command]
        return self._results.get(command, ExecResult(exit_code=0, stdout="ok"))

    def capture_diff(self, handle):
        raise NotImplementedError

    def destroy(self, handle):
        pass


HANDLE = SandboxHandle(sandbox_id="sbx-1", workdir="/work", base_commit="abc")


def test_string_form_success_exit_code_maps_to_success():
    sandbox = FakeSandbox({"lint": ExecResult(exit_code=0, stdout="all good")})
    results = run_local_checks(sandbox, HANDLE, {"lint": "lint"})
    assert results["lint"].conclusion == "success"
    assert "all good" in results["lint"].detail


def test_string_form_nonzero_exit_code_maps_to_failure():
    sandbox = FakeSandbox({"tests": ExecResult(exit_code=1, stdout="", stderr="AssertionError")})
    results = run_local_checks(sandbox, HANDLE, {"tests": "tests"})
    assert results["tests"].conclusion == "failure"
    assert "AssertionError" in results["tests"].detail


def test_runs_with_network_disabled():
    sandbox = FakeSandbox()
    run_local_checks(sandbox, HANDLE, {"lint": "lint"})
    assert sandbox.calls[0]["network"] is False


def test_sandbox_exception_is_reported_as_failure_not_dropped():
    sandbox = FakeSandbox(raises={"tests": TimeoutError("boom")})
    results = run_local_checks(sandbox, HANDLE, {"tests": "tests"})
    assert results["tests"].conclusion == "failure"
    assert "TimeoutError" in results["tests"].detail


def test_long_output_is_trimmed():
    sandbox = FakeSandbox({"lint": ExecResult(exit_code=0, stdout="x" * 10_000)})
    results = run_local_checks(sandbox, HANDLE, {"lint": "lint"})
    assert len(results["lint"].detail) < 10_000
    assert "truncated" in results["lint"].detail


def test_multiple_checks_all_run():
    sandbox = FakeSandbox({"lint": ExecResult(exit_code=0), "types": ExecResult(exit_code=1)})
    results = run_local_checks(sandbox, HANDLE, {"lint": "lint", "types": "types"})
    assert results["lint"].conclusion == "success"
    assert results["types"].conclusion == "failure"


def test_invalid_spec_type_raises():
    sandbox = FakeSandbox()
    with pytest.raises(TypeError):
        run_local_checks(sandbox, HANDLE, {"lint": 123})


def test_object_form_without_command_key_raises():
    sandbox = FakeSandbox()
    with pytest.raises(ValueError):
        run_local_checks(sandbox, HANDLE, {"lint": {"junit": "report.xml"}})


# -- object form: junit-backed checks (R-A7) --------------------------------------------

JUNIT_OK = """<?xml version="1.0"?>
<testsuite tests="3" failures="0" errors="0"><testcase name="a"/></testsuite>"""

JUNIT_WITH_FAILURES = """<?xml version="1.0"?>
<testsuite tests="3" failures="1" errors="0"><testcase name="a"/></testsuite>"""

JUNIT_ZERO_TESTS = """<?xml version="1.0"?>
<testsuite tests="0" failures="0" errors="0"></testsuite>"""

JUNIT_NESTED = """<?xml version="1.0"?>
<testsuites><testsuite tests="2" failures="0" errors="0"/><testsuite tests="1" failures="0" errors="0"/></testsuites>"""


def test_junit_form_passes_when_report_is_green(tmp_path):
    (tmp_path / "report.xml").write_text(JUNIT_OK)
    handle = SandboxHandle(sandbox_id="s", workdir=str(tmp_path), base_commit="abc")
    sandbox = FakeSandbox({"unit": ExecResult(exit_code=0)})
    results = run_local_checks(sandbox, handle, {"unit": {"command": "unit", "junit": "report.xml"}})
    assert results["unit"].conclusion == "success"


def test_junit_form_supports_nested_testsuites_element(tmp_path):
    (tmp_path / "report.xml").write_text(JUNIT_NESTED)
    handle = SandboxHandle(sandbox_id="s", workdir=str(tmp_path), base_commit="abc")
    sandbox = FakeSandbox({"unit": ExecResult(exit_code=0)})
    results = run_local_checks(sandbox, handle, {"unit": {"command": "unit", "junit": "report.xml"}})
    assert results["unit"].conclusion == "success"


def test_junit_form_fails_on_reported_failures_even_with_exit_0(tmp_path):
    (tmp_path / "report.xml").write_text(JUNIT_WITH_FAILURES)
    handle = SandboxHandle(sandbox_id="s", workdir=str(tmp_path), base_commit="abc")
    sandbox = FakeSandbox({"unit": ExecResult(exit_code=0)})
    results = run_local_checks(sandbox, handle, {"unit": {"command": "unit", "junit": "report.xml"}})
    assert results["unit"].conclusion == "failure"
    assert "1 failure" in results["unit"].detail


def test_junit_form_fails_on_zero_tests_even_with_exit_0(tmp_path):
    (tmp_path / "report.xml").write_text(JUNIT_ZERO_TESTS)
    handle = SandboxHandle(sandbox_id="s", workdir=str(tmp_path), base_commit="abc")
    sandbox = FakeSandbox({"unit": ExecResult(exit_code=0)})
    results = run_local_checks(sandbox, handle, {"unit": {"command": "unit", "junit": "report.xml"}})
    assert results["unit"].conclusion == "failure"
    assert "0 tests" in results["unit"].detail


def test_junit_form_enforces_min_tests(tmp_path):
    (tmp_path / "report.xml").write_text(JUNIT_OK)  # 3 tests
    handle = SandboxHandle(sandbox_id="s", workdir=str(tmp_path), base_commit="abc")
    sandbox = FakeSandbox({"unit": ExecResult(exit_code=0)})
    results = run_local_checks(sandbox, handle,
                                {"unit": {"command": "unit", "junit": "report.xml", "min_tests": 10}})
    assert results["unit"].conclusion == "failure"
    assert "fewer than the required 10" in results["unit"].detail


def test_junit_form_fails_when_exit_code_is_nonzero_regardless_of_report(tmp_path):
    (tmp_path / "report.xml").write_text(JUNIT_OK)
    handle = SandboxHandle(sandbox_id="s", workdir=str(tmp_path), base_commit="abc")
    sandbox = FakeSandbox({"unit": ExecResult(exit_code=1, stderr="boom")})
    results = run_local_checks(sandbox, handle, {"unit": {"command": "unit", "junit": "report.xml"}})
    assert results["unit"].conclusion == "failure"
    assert "boom" in results["unit"].detail


def test_junit_form_fails_when_report_is_malformed(tmp_path):
    (tmp_path / "report.xml").write_text("not xml at all")
    handle = SandboxHandle(sandbox_id="s", workdir=str(tmp_path), base_commit="abc")
    sandbox = FakeSandbox({"unit": ExecResult(exit_code=0)})
    results = run_local_checks(sandbox, handle, {"unit": {"command": "unit", "junit": "report.xml"}})
    assert results["unit"].conclusion == "failure"
    assert "unparseable" in results["unit"].detail


def test_junit_form_fails_when_report_is_missing_exit_0_no_report_gaming(tmp_path):
    """The R-A7 scenario: exit code 0, but no report was ever written."""
    handle = SandboxHandle(sandbox_id="s", workdir=str(tmp_path), base_commit="abc")
    sandbox = FakeSandbox({"unit": ExecResult(exit_code=0, stdout="OK")})
    results = run_local_checks(sandbox, handle, {"unit": {"command": "unit", "junit": "report.xml"}})
    assert results["unit"].conclusion == "failure"
    assert "no JUnit report" in results["unit"].detail


# -- real end-to-end gaming proof, via LocalSandbox --------------------------------------

def test_os_exit_0_runner_with_no_junit_report_fails_end_to_end(repo_mirror):
    """A real check script that calls os._exit(0) -- skipping unittest's own exit-code
    aggregation and any atexit/finally cleanup that would normally run -- still exits
    process code 0. Without the JUnit-backed object form, that alone would make the
    check pass no matter what the "tests" actually did. It must not."""
    sandbox = LocalSandbox(repos_root=repo_mirror["repos_root"], sandbox_root=repo_mirror["sandbox_root"])
    handle = sandbox.create(repo_mirror["repo"], repo_mirror["base_commit"])

    gaming_script = "import os\nos._exit(0)\n"
    (Path(handle.workdir) / "gamed_runner.py").write_text(gaming_script)

    results = run_local_checks(sandbox, handle, {
        "unit": {"command": "python3 gamed_runner.py", "junit": "report.xml"},
    })

    assert results["unit"].conclusion == "failure"
    assert "no JUnit report" in results["unit"].detail


def test_os_exit_0_runner_without_junit_configured_is_the_pre_existing_gap(repo_mirror):
    """Documents why the string form must not be used for anything that can game its
    own exit code: without `junit` configured, the same os._exit(0) script passes."""
    sandbox = LocalSandbox(repos_root=repo_mirror["repos_root"], sandbox_root=repo_mirror["sandbox_root"])
    handle = sandbox.create(repo_mirror["repo"], repo_mirror["base_commit"])

    gaming_script = "import os\nos._exit(0)\n"
    (Path(handle.workdir) / "gamed_runner.py").write_text(gaming_script)

    results = run_local_checks(sandbox, handle, {"unit": "python3 gamed_runner.py"})

    assert results["unit"].conclusion == "success"  # exactly why the object+junit form exists
