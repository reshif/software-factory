"""In-memory `SandboxPort` for tests and `factory demo` that don't need a real
git checkout or Docker at all.

Unlike `LocalSandbox`/`DockerSandbox`, `FakeSandbox` doesn't clone anything:
`create` just allocates a plain temp directory a `FakeRuntime` script can
write into, `exec` returns a scripted or canned `ExecResult`, and
`capture_diff` returns whatever `set_diff` last recorded for that sandbox
(or an empty `Diff` if nothing was scripted).
"""
from __future__ import annotations

import shutil
import tempfile
import uuid
from collections.abc import Callable

from ..models import Diff, ExecResult, SandboxHandle

ExecScript = Callable[[SandboxHandle, list[str]], ExecResult]

_DEFAULT_EXEC_RESULT = ExecResult(exit_code=0, stdout="", stderr="")


class FakeSandbox:
    """`SandboxPort` with no real isolation and no real git: pure test/demo scaffolding."""

    def __init__(self, *, exec_script: ExecScript | None = None):
        self._exec_script = exec_script
        self._handles: dict[str, SandboxHandle] = {}
        self._diffs: dict[str, Diff] = {}

    def create(self, repo: str, base_commit: str) -> SandboxHandle:
        sandbox_id = uuid.uuid4().hex
        workdir = tempfile.mkdtemp(prefix=f"fake-sandbox-{sandbox_id}-")
        handle = SandboxHandle(sandbox_id=sandbox_id, workdir=workdir, base_commit=base_commit)
        self._handles[sandbox_id] = handle
        return handle

    def exec(self, handle: SandboxHandle, cmd: list[str], *, network: bool = False, timeout_s: int = 900) -> ExecResult:
        if self._exec_script is not None:
            return self._exec_script(handle, cmd)
        return _DEFAULT_EXEC_RESULT

    def set_diff(self, sandbox_id: str, diff: Diff) -> None:
        """Test hook: script what `capture_diff` returns for this sandbox."""
        self._diffs[sandbox_id] = diff

    def capture_diff(self, handle: SandboxHandle) -> Diff:
        return self._diffs.get(handle.sandbox_id, Diff(base_commit=handle.base_commit, changes=(), patch=""))

    def destroy(self, handle: SandboxHandle) -> None:
        shutil.rmtree(handle.workdir, ignore_errors=True)
        self._handles.pop(handle.sandbox_id, None)
        self._diffs.pop(handle.sandbox_id, None)
