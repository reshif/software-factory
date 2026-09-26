from factory.models import ExecResult, SandboxHandle
from factory.verification.local_checks import run_local_checks


class FakeSandbox:
    """Minimal SandboxPort fake: scripted exec results, records the calls made."""

    def __init__(self, results: dict[str, ExecResult] | None = None, raises: dict[str, Exception] | None = None):
        self._results = results or {}
        self._raises = raises or {}
        self.calls: list[dict] = []

    def create(self, repo, base_commit):
        return SandboxHandle(sandbox_id="sbx-1", workdir="/work", base_commit=base_commit)

    def exec(self, handle, cmd, *, network=False, timeout_s=900):
        name = cmd[0]
        self.calls.append({"cmd": cmd, "network": network, "timeout_s": timeout_s})
        if name in self._raises:
            raise self._raises[name]
        return self._results.get(name, ExecResult(exit_code=0, stdout="ok"))

    def capture_diff(self, handle):
        raise NotImplementedError

    def destroy(self, handle):
        pass


HANDLE = SandboxHandle(sandbox_id="sbx-1", workdir="/work", base_commit="abc")


def test_success_exit_code_maps_to_success():
    sandbox = FakeSandbox({"lint": ExecResult(exit_code=0, stdout="all good")})
    results = run_local_checks(sandbox, HANDLE, {"lint": ["lint"]})
    assert results["lint"].conclusion == "success"
    assert "all good" in results["lint"].detail


def test_nonzero_exit_code_maps_to_failure():
    sandbox = FakeSandbox({"tests": ExecResult(exit_code=1, stdout="", stderr="AssertionError")})
    results = run_local_checks(sandbox, HANDLE, {"tests": ["tests"]})
    assert results["tests"].conclusion == "failure"
    assert "AssertionError" in results["tests"].detail


def test_runs_with_network_disabled():
    sandbox = FakeSandbox()
    run_local_checks(sandbox, HANDLE, {"lint": ["lint"]})
    assert sandbox.calls[0]["network"] is False


def test_sandbox_exception_is_reported_as_failure_not_dropped():
    sandbox = FakeSandbox(raises={"tests": TimeoutError("boom")})
    results = run_local_checks(sandbox, HANDLE, {"tests": ["tests"]})
    assert results["tests"].conclusion == "failure"
    assert "TimeoutError" in results["tests"].detail


def test_long_output_is_trimmed():
    sandbox = FakeSandbox({"lint": ExecResult(exit_code=0, stdout="x" * 10_000)})
    results = run_local_checks(sandbox, HANDLE, {"lint": ["lint"]})
    assert len(results["lint"].detail) < 10_000
    assert "truncated" in results["lint"].detail


def test_multiple_checks_all_run():
    sandbox = FakeSandbox({
        "lint": ExecResult(exit_code=0),
        "types": ExecResult(exit_code=1),
    })
    results = run_local_checks(sandbox, HANDLE, {"lint": ["lint"], "types": ["types"]})
    assert results["lint"].conclusion == "success"
    assert results["types"].conclusion == "failure"
