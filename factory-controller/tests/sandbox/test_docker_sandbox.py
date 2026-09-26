"""DockerSandbox tests: real docker, actually run (build spec §1 rule 7).

Skipped unless `docker info` succeeds. When it does, this module:
  1. builds a small throwaway image (proving the build path works, not just
     the `docker run` invocation),
  2. proves `network=False` (the default) leaves the container with no
     network beyond loopback,
  3. proves `network=True` attaches the container to the configured egress
     network, and
  4. cleans up every image, container and network it created.

`docker run --rm` already removes each container on exit; the fixtures below
additionally remove the image and the egress network so nothing is left
behind (build spec §1 rule 8).
"""
from __future__ import annotations

import shutil
import subprocess
import uuid

import pytest

from factory.sandbox.docker import DockerSandbox


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        subprocess.run(["docker", "info"], capture_output=True, timeout=10, check=True)
        return True
    except Exception:
        return False


pytestmark = [
    pytest.mark.docker,
    pytest.mark.skipif(not _docker_available(), reason="docker is not available"),
]


@pytest.fixture(scope="module")
def sandbox_image(tmp_path_factory):
    """Build a minimal, non-root image for these tests and remove it afterwards."""
    build_dir = tmp_path_factory.mktemp("sandbox-image")
    dockerfile = build_dir / "Dockerfile"
    dockerfile.write_text(
        "FROM alpine:latest\n"
        "RUN adduser -D -u 1000 sandbox\n"
        "USER sandbox\n"
    )
    tag = f"factory-sandbox-pytest:{uuid.uuid4().hex[:12]}"
    subprocess.run(
        ["docker", "build", "-t", tag, "-f", str(dockerfile), str(build_dir)],
        check=True, capture_output=True, text=True,
    )
    try:
        yield tag
    finally:
        subprocess.run(["docker", "rmi", "-f", tag], capture_output=True, text=True)


@pytest.fixture
def egress_network():
    """A throwaway bridge network standing in for `factory-egress`, removed afterwards."""
    name = f"factory-egress-pytest-{uuid.uuid4().hex[:12]}"
    subprocess.run(["docker", "network", "create", name], check=True, capture_output=True, text=True)
    try:
        yield name
    finally:
        subprocess.run(["docker", "network", "rm", name], capture_output=True, text=True)


def test_create_clones_and_checks_out_base_commit(repo_mirror, sandbox_image):
    sandbox = DockerSandbox(image=sandbox_image, repos_root=repo_mirror["repos_root"],
                             sandbox_root=repo_mirror["sandbox_root"])
    handle = sandbox.create(repo_mirror["repo"], repo_mirror["base_commit"])
    try:
        assert handle.base_commit == repo_mirror["base_commit"]
        import os
        assert os.path.exists(os.path.join(handle.workdir, "README.md"))
    finally:
        sandbox.destroy(handle)


def test_exec_runs_in_the_container(repo_mirror, sandbox_image):
    sandbox = DockerSandbox(image=sandbox_image, repos_root=repo_mirror["repos_root"],
                             sandbox_root=repo_mirror["sandbox_root"])
    handle = sandbox.create(repo_mirror["repo"], repo_mirror["base_commit"])
    try:
        result = sandbox.exec(handle, ["cat", "README.md"])
        assert result.exit_code == 0
        assert result.stdout == "hello\n"
    finally:
        sandbox.destroy(handle)


def test_network_false_has_no_network_beyond_loopback(repo_mirror, sandbox_image):
    """The default: code execution gets no network at all (final draft §13.1 #2)."""
    sandbox = DockerSandbox(image=sandbox_image, repos_root=repo_mirror["repos_root"],
                             sandbox_root=repo_mirror["sandbox_root"])
    handle = sandbox.create(repo_mirror["repo"], repo_mirror["base_commit"])
    try:
        result = sandbox.exec(handle, ["cat", "/proc/net/dev"], network=False)
        assert result.exit_code == 0
        assert "lo:" in result.stdout
        assert "eth0:" not in result.stdout
    finally:
        sandbox.destroy(handle)


def test_network_true_attaches_to_the_egress_network(repo_mirror, sandbox_image, egress_network):
    sandbox = DockerSandbox(image=sandbox_image, repos_root=repo_mirror["repos_root"],
                             sandbox_root=repo_mirror["sandbox_root"], egress_network=egress_network,
                             egress_proxy_url="http://egress-proxy:8888")
    handle = sandbox.create(repo_mirror["repo"], repo_mirror["base_commit"])
    try:
        result = sandbox.exec(handle, ["cat", "/proc/net/dev"], network=True)
        assert result.exit_code == 0
        assert "eth0:" in result.stdout
    finally:
        sandbox.destroy(handle)


def test_no_credentials_are_mounted(repo_mirror, sandbox_image):
    """`exec` only ever mounts the sandbox workdir; nothing under it is a credential."""
    sandbox = DockerSandbox(image=sandbox_image, repos_root=repo_mirror["repos_root"],
                             sandbox_root=repo_mirror["sandbox_root"])
    handle = sandbox.create(repo_mirror["repo"], repo_mirror["base_commit"])
    try:
        result = sandbox.exec(handle, ["sh", "-c", "ls -la /root 2>&1; echo ---; env"])
        assert "GITHUB_TOKEN" not in result.stdout
        assert "ANTHROPIC_API_KEY" not in result.stdout
    finally:
        sandbox.destroy(handle)


def test_exec_runs_as_non_root_user(repo_mirror, sandbox_image):
    sandbox = DockerSandbox(image=sandbox_image, repos_root=repo_mirror["repos_root"],
                             sandbox_root=repo_mirror["sandbox_root"])
    handle = sandbox.create(repo_mirror["repo"], repo_mirror["base_commit"])
    try:
        result = sandbox.exec(handle, ["id", "-u"])
        assert result.stdout.strip() == "1000"
    finally:
        sandbox.destroy(handle)


def test_capture_diff_against_a_docker_sandbox_workdir(repo_mirror, sandbox_image):
    """capture_diff is pure host-side git, so it works the same for DockerSandbox."""
    sandbox = DockerSandbox(image=sandbox_image, repos_root=repo_mirror["repos_root"],
                             sandbox_root=repo_mirror["sandbox_root"])
    handle = sandbox.create(repo_mirror["repo"], repo_mirror["base_commit"])
    try:
        import os
        with open(os.path.join(handle.workdir, "src", "app.py"), "a") as f:
            f.write("# a change\n")
        diff = sandbox.capture_diff(handle)
        assert diff.paths == ("src/app.py",)
        assert diff.changes[0].status == "modified"
        assert "a change" in diff.changes[0].added_text
    finally:
        sandbox.destroy(handle)


def test_destroy_removes_the_workdir(repo_mirror, sandbox_image):
    import os
    sandbox = DockerSandbox(image=sandbox_image, repos_root=repo_mirror["repos_root"],
                             sandbox_root=repo_mirror["sandbox_root"])
    handle = sandbox.create(repo_mirror["repo"], repo_mirror["base_commit"])
    assert os.path.isdir(handle.workdir)
    sandbox.destroy(handle)
    assert not os.path.exists(handle.workdir)
