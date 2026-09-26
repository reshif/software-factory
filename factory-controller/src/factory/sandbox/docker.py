"""Container-per-task sandbox (final draft §12.2 M8, §13.1 #2).

Every task gets its own throwaway container. `exec` defaults to
`--network none`: code execution is treated as arbitrary code, so it runs
with no network and no credentials. When a task legitimately needs egress
(installing dependencies), the controller passes `network=True` and the
container instead joins the `factory-egress` docker network, whose proxy
(deploy/egress-proxy/) allows only the LLM gateway and a package mirror.
Nothing under this module ever mounts credentials into the container.
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


class DockerSandbox:
    """`SandboxPort` backed by `docker run` with network off by default."""

    def __init__(
        self,
        *,
        image: str,
        repos_root: str,
        sandbox_root: str,
        egress_network: str = "factory-egress",
        egress_proxy_url: str | None = None,
        docker_bin: str = "docker",
    ):
        self._image = image
        self._repos_root = repos_root
        self._sandbox_root = sandbox_root
        self._egress_network = egress_network
        self._egress_proxy_url = egress_proxy_url
        self._docker_bin = docker_bin
        os.makedirs(sandbox_root, exist_ok=True)

    def _mirror_path(self, repo: str) -> str:
        return os.path.join(self._repos_root, repo.replace("/", "__"))

    def create(self, repo: str, base_commit: str) -> SandboxHandle:
        """A clean checkout of `base_commit`, cloned from the local mirror in `repos_root`."""
        mirror = self._mirror_path(repo)
        if not os.path.isdir(mirror):
            raise FileNotFoundError(
                f"no local mirror for {repo!r} at {mirror!r}; the github module clones it into repos_root"
            )
        sandbox_id = uuid.uuid4().hex
        workdir = os.path.join(self._sandbox_root, sandbox_id)
        subprocess.run(["git", "clone", "--quiet", mirror, workdir], check=True, capture_output=True, text=True)
        subprocess.run(["git", "-C", workdir, "checkout", "--quiet", base_commit], check=True, capture_output=True, text=True)
        return SandboxHandle(sandbox_id=sandbox_id, workdir=workdir, base_commit=base_commit)

    def exec(self, handle: SandboxHandle, cmd: list[str], *, network: bool = False, timeout_s: int = 900) -> ExecResult:
        docker_cmd = [
            self._docker_bin, "run", "--rm",
            "--user", "1000:1000",
            "--read-only",
            "--tmpfs", "/tmp:rw,size=256m",
            "-v", f"{handle.workdir}:/work",
            "-w", "/work",
        ]
        if network:
            docker_cmd += ["--network", self._egress_network]
            if self._egress_proxy_url:
                docker_cmd += [
                    "-e", f"HTTP_PROXY={self._egress_proxy_url}",
                    "-e", f"HTTPS_PROXY={self._egress_proxy_url}",
                ]
        else:
            docker_cmd += ["--network", "none"]
        docker_cmd.append(self._image)
        docker_cmd.extend(cmd)

        start = time.monotonic()
        try:
            proc = subprocess.run(docker_cmd, capture_output=True, text=True, timeout=timeout_s)
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
