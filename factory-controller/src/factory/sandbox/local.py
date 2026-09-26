"""Subprocess-based sandbox for `factory demo` and tests.

**NOT A SECURITY BOUNDARY.** `exec` runs the command as the controller's own
OS user with no container, no filesystem isolation and no network control.
It exists so the demo and the test suite can exercise the `SandboxPort`
contract without Docker. Real agent runs always go through `DockerSandbox`.

`create`/`capture_diff`/`destroy` come from `CheckoutSandbox`, so this is
just as protected against a hostile checkout (`.git/config`, `.gitattributes`
filters) as `DockerSandbox` is (see `sandbox/base.py`) — it just doesn't
sandbox the *command* it runs.
"""
from __future__ import annotations

import logging
import subprocess
import time

from ..models import ExecResult, SandboxHandle
from .base import CheckoutSandbox

logger = logging.getLogger(__name__)


class LocalSandbox(CheckoutSandbox):
    """`SandboxPort` backed by a plain checkout and `subprocess.run`. Not isolated."""

    def __init__(self, *, repos_root: str, sandbox_root: str):
        super().__init__(repos_root=repos_root, sandbox_root=sandbox_root)
        logger.warning(
            "LocalSandbox is NOT a security boundary: commands run as the controller's "
            "own user with no container isolation and no network control. Use "
            "DockerSandbox for anything touching untrusted agent output."
        )

    def exec(self, handle: SandboxHandle, cmd: list[str], *, network: bool = False, timeout_s: int = 900) -> ExecResult:
        if network:
            logger.warning("LocalSandbox ignores network=True: it has no egress control, so this is a no-op")
        start = time.monotonic()
        try:
            proc = subprocess.run(cmd, cwd=handle.workdir, capture_output=True, text=True, timeout=timeout_s)
            return ExecResult(exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr,
                               duration_s=time.monotonic() - start)
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            return ExecResult(exit_code=124, stdout=stdout, stderr=stderr + f"\ntimed out after {timeout_s}s",
                               duration_s=time.monotonic() - start)
