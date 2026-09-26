"""Shared checkout lifecycle for every real `SandboxPort` implementation.

`create`, `capture_diff` and `destroy` are identical between `DockerSandbox`
and `LocalSandbox`; only how a command actually runs (`exec`) differs, so
that shared part lives here once instead of being duplicated per sandbox.

The git repository for each checkout is created OUTSIDE the mounted/executed
workdir, under `sandbox_root/<id>.git`, via `git clone --separate-git-dir`.
The `.git` pointer file `clone` leaves behind inside the workdir is deleted
immediately. From then on nothing an agent writes into the workdir —
including a `.git/config` with a hostile `core.hooksPath`/`core.fsmonitor`/
`core.pager`, or a `.gitattributes` filter driver — can reach a git command
this controller runs on the host: every call goes through `gitcmd.run_git`
with `--git-dir`/`--work-tree` pointing explicitly outside the workdir, and
never lets git discover a repository by walking up from it (red team R-A1).
"""
from __future__ import annotations

import logging
import os
import shutil
import uuid

from .. import gitcmd
from ..models import Diff, SandboxHandle
from . import gitutils

logger = logging.getLogger(__name__)


class CheckoutSandbox:
    """Base for sandboxes backed by a mirrored git checkout. Subclasses implement `exec`."""

    def __init__(self, *, repos_root: str, sandbox_root: str):
        self._repos_root = repos_root
        self._sandbox_root = sandbox_root
        os.makedirs(sandbox_root, exist_ok=True)

    def _mirror_path(self, repo: str) -> str:
        return os.path.join(self._repos_root, repo.replace("/", "__"))

    def _git_dir(self, sandbox_id: str) -> str:
        return os.path.join(self._sandbox_root, f"{sandbox_id}.git")

    def create(self, repo: str, base_commit: str) -> SandboxHandle:
        """A clean checkout of `base_commit`, cloned from the local mirror in `repos_root`."""
        mirror = self._mirror_path(repo)
        if not os.path.isdir(mirror):
            raise FileNotFoundError(f"no local mirror for {repo!r} at {mirror!r}")
        sandbox_id = uuid.uuid4().hex
        workdir = os.path.join(self._sandbox_root, sandbox_id)
        git_dir = self._git_dir(sandbox_id)
        try:
            gitcmd.run_git(
                ["clone", "--quiet", "--no-hardlinks", f"--separate-git-dir={git_dir}", mirror, workdir],
                cwd=self._sandbox_root,
            )
            # Never leave a `.git` pointer file inside the executed workdir: every later
            # git call passes --git-dir/--work-tree explicitly instead of discovering one.
            pointer = os.path.join(workdir, ".git")
            if os.path.exists(pointer):
                os.remove(pointer)
            gitcmd.run_git(["checkout", "--quiet", base_commit], cwd=workdir, git_dir=git_dir, work_tree=workdir)
        except Exception:
            shutil.rmtree(workdir, ignore_errors=True)
            shutil.rmtree(git_dir, ignore_errors=True)
            raise
        return SandboxHandle(sandbox_id=sandbox_id, workdir=workdir, base_commit=base_commit)

    def capture_diff(self, handle: SandboxHandle) -> Diff:
        return gitutils.build_diff(self._git_dir(handle.sandbox_id), handle.workdir, handle.base_commit)

    def destroy(self, handle: SandboxHandle) -> None:
        shutil.rmtree(handle.workdir, ignore_errors=True)
        shutil.rmtree(self._git_dir(handle.sandbox_id), ignore_errors=True)

    def exec(self, handle: SandboxHandle, cmd: list[str], *, network: bool = False, timeout_s: int = 900):
        raise NotImplementedError
