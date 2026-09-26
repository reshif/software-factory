"""Subprocess-based sandbox for `factory demo` and tests.

**NOT A SECURITY BOUNDARY.** `exec` runs the command as the controller's own
OS user with no container, no filesystem isolation and no network control.
It exists so the demo and the test suite can exercise the `SandboxPort`
contract without Docker. Real agent runs always go through `DockerSandbox`.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
import uuid

from ..models import Diff, ExecResult, SandboxHandle
from . import gitutils

logger = logging.getLogger(__name__)


class LocalSandbox:
    """`SandboxPort` backed by a plain checkout and `subprocess.run`. Not isolated."""

    def __init__(self, *, repos_root: str, sandbox_root: str):
        self._repos_root = repos_root
        self._sandbox_root = sandbox_root
        os.makedirs(sandbox_root, exist_ok=True)
        logger.warning(
            "LocalSandbox is NOT a security boundary: commands run as the controller's "
            "own user with no container isolation and no network control. Use "
            "DockerSandbox for anything touching untrusted agent output."
        )

    def _mirror_path(self, repo: str) -> str:
        return os.path.join(self._repos_root, repo.replace("/", "__"))

    def create(self, repo: str, base_commit: str) -> SandboxHandle:
        mirror = self._mirror_path(repo)
        if not os.path.isdir(mirror):
            raise FileNotFoundError(f"no local mirror for {repo!r} at {mirror!r}")
        sandbox_id = uuid.uuid4().hex
        workdir = os.path.join(self._sandbox_root, sandbox_id)
        subprocess.run(["git", "clone", "--quiet", mirror, workdir], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", workdir, "checkout", "--quiet", base_commit], check=True, capture_output=True, text=True)
        return SandboxHandle(sandbox_id=sandbox_id, workdir=workdir, base_commit=base_commit)

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

    def capture_diff(self, handle: SandboxHandle) -> Diff:
        return gitutils.build_diff(handle.workdir, handle.base_commit)

    def destroy(self, handle: SandboxHandle) -> None:
        shutil.rmtree(handle.workdir, ignore_errors=True)
