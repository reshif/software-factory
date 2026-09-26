"""Run named local checks (lint, types, unit tests, ...) in the sandbox (final draft §9.2).

Code execution defaults to no network access (§13.1 #2): every command runs through
`SandboxPort.exec(..., network=False)`.
"""
import logging

from ..models import CheckResult, ExecResult, SandboxHandle
from ..ports import SandboxPort

logger = logging.getLogger(__name__)

MAX_DETAIL_CHARS = 4000


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


def run_local_checks(sandbox: SandboxPort, handle: SandboxHandle, commands: dict[str, list[str]],
                      *, timeout_s: int = 900) -> dict[str, CheckResult]:
    """Run each named command in `commands` through the sandbox with network disabled.

    Returns a mapping of check name -> CheckResult. A nonzero exit code is a
    "failure" conclusion; a zero exit code is "success". Any exception raised by
    the sandbox itself (timeout, crash, ...) is also reported as "failure" --
    never silently dropped -- so the caller's fail-closed evaluation sees it.
    """
    results: dict[str, CheckResult] = {}
    for name, cmd in commands.items():
        try:
            exec_result = sandbox.exec(handle, cmd, network=False, timeout_s=timeout_s)
        except Exception as exc:  # noqa: BLE001 -- fail closed, never let this escape as "missing"
            logger.warning("local check %r raised %s", name, exc)
            results[name] = CheckResult(name, "failure", detail=_trim(f"{type(exc).__name__}: {exc}"))
            continue
        conclusion = "success" if exec_result.exit_code == 0 else "failure"
        results[name] = CheckResult(name, conclusion, detail=_detail(exec_result))
    return results
