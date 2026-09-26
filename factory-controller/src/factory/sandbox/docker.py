"""Container-per-task sandbox (final draft §12.2 M8, §13.1 #2).

Every task gets its own throwaway, named container. `exec` defaults to
`--network none`: code execution is treated as arbitrary code, so it runs
with no network and no credentials. When a task legitimately needs egress
(installing dependencies), the controller passes `network=True` and the
container instead joins the `factory-egress` docker network, whose proxy
(deploy/egress-proxy/) allows only the LLM gateway and a package mirror.
Nothing under this module ever mounts credentials into the container.

Every container is named and started with `--init`; if the host-side
`docker run` call itself times out, the container is force-removed by that
name (`docker rm -f`) rather than left running — `--rm`/`docker run`'s own
timeout kills only the *client* process, not the container the daemon is
still running (code review Q-H3). Resource limits (`--pids-limit`,
`--memory`, `--cpus`) and the in-container UID/GID all have safe defaults
but are constructor arguments so a deployment can tune them.

`create`/`capture_diff`/`destroy` come from `CheckoutSandbox` (`sandbox/base.py`).
"""
from __future__ import annotations

import logging
import subprocess
import time
import uuid

from ..models import ExecResult, SandboxHandle
from .base import CheckoutSandbox

logger = logging.getLogger(__name__)


class DockerSandbox(CheckoutSandbox):
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
        uid: int = 1000,
        gid: int = 1000,
        pids_limit: int = 256,
        memory: str = "512m",
        cpus: str = "2.0",
    ):
        super().__init__(repos_root=repos_root, sandbox_root=sandbox_root)
        self._image = image
        self._egress_network = egress_network
        self._egress_proxy_url = egress_proxy_url
        self._docker_bin = docker_bin
        self._uid = uid
        self._gid = gid
        self._pids_limit = pids_limit
        self._memory = memory
        self._cpus = cpus

    def _container_name(self, handle: SandboxHandle) -> str:
        return f"factory-sbx-{handle.sandbox_id[:16]}-{uuid.uuid4().hex[:8]}"

    def exec(self, handle: SandboxHandle, cmd: list[str], *, network: bool = False, timeout_s: int = 900) -> ExecResult:
        name = self._container_name(handle)
        docker_cmd = [
            self._docker_bin, "run", "--rm", "--init",
            "--name", name,
            "--user", f"{self._uid}:{self._gid}",
            "--read-only",
            "--tmpfs", "/tmp:rw,size=256m",
            "--pids-limit", str(self._pids_limit),
            "--memory", self._memory,
            "--cpus", self._cpus,
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
            self._force_remove(name)
            stdout = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode() if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            return ExecResult(exit_code=124, stdout=stdout, stderr=stderr + f"\ntimed out after {timeout_s}s",
                               duration_s=time.monotonic() - start)

    def _force_remove(self, name: str) -> None:
        """`docker run`'s own timeout only kills our client process; the daemon keeps the
        container running until something explicitly removes it (code review Q-H3)."""
        try:
            subprocess.run([self._docker_bin, "rm", "-f", name], capture_output=True, text=True, timeout=30)
        except Exception:
            logger.warning("failed to force-remove timed-out container %s", name, exc_info=True)
