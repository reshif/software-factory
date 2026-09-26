"""Run named local checks (lint, types, unit tests, ...) in the sandbox (final draft §9.2).

Code execution defaults to no network access (§13.1 #2): every command runs through
`SandboxPort.exec(..., network=False)`.

Each entry in `factory.yaml`'s `checks` map (`factory-kit/schemas/factory.schema.json`)
is either a plain shell command string, or an object ``{"command": ..., "junit": ...,
"min_tests": ...}``. The object form defends against a check that games a bare exit
code: a test runner that calls ``os._exit(0)`` (skipping normal interpreter shutdown,
and with it anything that would have made the run fail) still exits 0, and a
string-only check would wrongly pass. When `junit` is set, the check instead passes
only if the command exits 0 **and** the named JUnit XML report exists in the sandbox
workdir, parses, reports more than zero tests (and at least `min_tests`, default 1),
and has zero failures and zero errors. A missing, empty or unparseable report is a
failure -- fail closed, never "trust the exit code".
"""
import logging
import xml.etree.ElementTree as ET
from pathlib import Path

from ..models import CheckResult, ExecResult, SandboxHandle
from ..ports import SandboxPort

logger = logging.getLogger(__name__)

MAX_DETAIL_CHARS = 4000
DEFAULT_MIN_TESTS = 1


def _trim(text: str, limit: int = MAX_DETAIL_CHARS) -> str:
    """Keep the tail of `text`, which is almost always where the useful error is."""
    if len(text) <= limit:
        return text
    return f"[truncated {len(text) - limit} chars]\n" + text[-limit:]


def _detail(result: ExecResult) -> str:
    parts = []
    if result.stdout.strip():
        parts.append(result.stdout.strip())
    if result.stderr.strip():
        parts.append(result.stderr.strip())
    return _trim("\n".join(parts))


def _normalize_spec(name: str, spec) -> tuple[str, str | None, int]:
    """Return (command, junit_path_or_None, min_tests) for one `checks` entry."""
    if isinstance(spec, str):
        return spec, None, DEFAULT_MIN_TESTS
    if isinstance(spec, dict):
        try:
            command = spec["command"]
        except KeyError:
            raise ValueError(f"check {name!r}: object form requires a 'command'") from None
        return command, spec.get("junit"), int(spec.get("min_tests", DEFAULT_MIN_TESTS))
    raise TypeError(f"check {name!r}: invalid spec {spec!r} (expected a string or an object)")


def _junit_counts(report_path: Path) -> tuple[int, int, int]:
    """Return (tests, failures, errors) summed over every <testsuite> in the report."""
    root = ET.parse(report_path).getroot()
    suites = [root] if root.tag == "testsuite" else root.findall(".//testsuite")
    if not suites:
        raise ValueError(f"{report_path}: no <testsuite> element found")
    tests = failures = errors = 0
    for suite in suites:
        tests += int(suite.get("tests", 0))
        failures += int(suite.get("failures", 0))
        errors += int(suite.get("errors", 0))
    return tests, failures, errors


def _verify_junit(workdir: str, junit: str, min_tests: int, exec_detail: str) -> CheckResult | None:
    """Return a failing CheckResult if the JUnit report doesn't back up a green exit code,
    or None if it does (caller fills in the "success" result)."""
    report_path = Path(workdir) / junit
    try:
        tests, failures, errors = _junit_counts(report_path)
    except FileNotFoundError:
        return CheckResult("", "failure",
                            detail=_trim(f"exit code was 0 but no JUnit report at {junit!r}\n{exec_detail}"))
    except (ET.ParseError, ValueError) as exc:
        return CheckResult("", "failure",
                            detail=_trim(f"JUnit report {junit!r} is unparseable: {exc}\n{exec_detail}"))
    if tests <= 0:
        return CheckResult("", "failure",
                            detail=_trim(f"JUnit report {junit!r} recorded 0 tests -- refusing to trust a "
                                         f"run that reports nothing\n{exec_detail}"))
    if tests < min_tests:
        return CheckResult("", "failure",
                            detail=_trim(f"JUnit report {junit!r} recorded {tests} test(s), fewer than the "
                                         f"required {min_tests}\n{exec_detail}"))
    if failures or errors:
        return CheckResult("", "failure",
                            detail=_trim(f"JUnit report {junit!r}: {failures} failure(s), {errors} "
                                         f"error(s) out of {tests} test(s)\n{exec_detail}"))
    return None


def run_local_checks(sandbox: SandboxPort, handle: SandboxHandle, commands: dict, *,
                      timeout_s: int = 900) -> dict[str, CheckResult]:
    """Run each named check in `commands` through the sandbox with network disabled.

    Returns a mapping of check name -> CheckResult. A nonzero exit code is a
    "failure" conclusion. Any exception raised by the sandbox itself (timeout,
    crash, ...) is also reported as "failure" -- never silently dropped -- so the
    caller's fail-closed evaluation sees it.
    """
    results: dict[str, CheckResult] = {}
    for name, spec in commands.items():
        command, junit, min_tests = _normalize_spec(name, spec)
        try:
            exec_result = sandbox.exec(handle, ["sh", "-c", command], network=False, timeout_s=timeout_s)
        except Exception as exc:  # noqa: BLE001 -- fail closed, never let this escape as "missing"
            logger.warning("local check %r raised %s", name, exc)
            results[name] = CheckResult(name, "failure", detail=_trim(f"{type(exc).__name__}: {exc}"))
            continue

        detail = _detail(exec_result)
        if exec_result.exit_code != 0:
            results[name] = CheckResult(name, "failure", detail=detail)
            continue
        if junit is None:
            results[name] = CheckResult(name, "success", detail=detail)
            continue

        failure = _verify_junit(handle.workdir, junit, min_tests, detail)
        results[name] = CheckResult(name, "failure", detail=failure.detail) if failure else \
            CheckResult(name, "success", detail=detail)
    return results
