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

import os
import shutil
import subprocess
import uuid

import pytest

from factory.policy.action_classes import classify
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


def test_uid_and_gid_are_constructor_arguments(repo_mirror, sandbox_image):
    sandbox = DockerSandbox(image=sandbox_image, repos_root=repo_mirror["repos_root"],
                             sandbox_root=repo_mirror["sandbox_root"], uid=1001, gid=1001)
    handle = sandbox.create(repo_mirror["repo"], repo_mirror["base_commit"])
    try:
        result = sandbox.exec(handle, ["sh", "-c", "id -u; id -g"])
        assert result.stdout.split() == ["1001", "1001"]
    finally:
        sandbox.destroy(handle)


def test_resource_limits_are_accepted_and_have_defaults(repo_mirror, sandbox_image):
    """pids_limit/memory/cpus are constructor args with sane defaults, and docker accepts them."""
    default_sandbox = DockerSandbox(image=sandbox_image, repos_root=repo_mirror["repos_root"],
                                     sandbox_root=repo_mirror["sandbox_root"])
    handle = default_sandbox.create(repo_mirror["repo"], repo_mirror["base_commit"])
    try:
        result = default_sandbox.exec(handle, ["echo", "ok"])
        assert result.exit_code == 0
    finally:
        default_sandbox.destroy(handle)

    tuned_sandbox = DockerSandbox(image=sandbox_image, repos_root=repo_mirror["repos_root"],
                                   sandbox_root=repo_mirror["sandbox_root"],
                                   pids_limit=32, memory="128m", cpus="0.5")
    handle2 = tuned_sandbox.create(repo_mirror["repo"], repo_mirror["base_commit"])
    try:
        result2 = tuned_sandbox.exec(handle2, ["echo", "ok"])
        assert result2.exit_code == 0
    finally:
        tuned_sandbox.destroy(handle2)


def test_exec_timeout_force_removes_the_leaked_container(repo_mirror, sandbox_image):
    """`docker run`'s client-side timeout doesn't stop the container the daemon keeps
    running; DockerSandbox must force-remove it itself (code review Q-H3)."""
    sandbox = DockerSandbox(image=sandbox_image, repos_root=repo_mirror["repos_root"],
                             sandbox_root=repo_mirror["sandbox_root"])
    handle = sandbox.create(repo_mirror["repo"], repo_mirror["base_commit"])
    try:
        result = sandbox.exec(handle, ["sleep", "30"], timeout_s=2)
        assert result.exit_code == 124

        listing = subprocess.run(
            ["docker", "ps", "-a", "--filter", f"name=factory-sbx-{handle.sandbox_id[:16]}",
             "--format", "{{.Names}}"],
            capture_output=True, text=True, check=True,
        )
        assert listing.stdout.strip() == ""
    finally:
        sandbox.destroy(handle)


def test_capture_diff_ignores_a_hostile_git_config_planted_in_the_workdir(repo_mirror, sandbox_image):
    """Same property as LocalSandbox (shared CheckoutSandbox/gitutils): a `.git/config`
    or hooks an agent writes into the workdir have zero effect on the host git
    invocation capture_diff runs (red team R-A1)."""
    sandbox = DockerSandbox(image=sandbox_image, repos_root=repo_mirror["repos_root"],
                             sandbox_root=repo_mirror["sandbox_root"])
    handle = sandbox.create(repo_mirror["repo"], repo_mirror["base_commit"])
    try:
        hostile_git_dir = os.path.join(handle.workdir, ".git")
        os.makedirs(hostile_git_dir)
        hooks_dir = os.path.join(handle.workdir, "evil-hooks")
        os.makedirs(hooks_dir)
        pager_marker = os.path.join(handle.workdir, "PAGER_RAN")
        hook_marker = os.path.join(handle.workdir, "HOOK_RAN")
        with open(os.path.join(hostile_git_dir, "config"), "w") as f:
            f.write(f"[core]\n\thooksPath = {hooks_dir}\n\tpager = touch {pager_marker}; cat\n")
        hook_path = os.path.join(hooks_dir, "post-index-change")
        with open(hook_path, "w") as f:
            f.write(f"#!/bin/sh\ntouch {hook_marker}\n")
        os.chmod(hook_path, 0o755)

        with open(os.path.join(handle.workdir, "src", "app.py"), "a") as f:
            f.write("# a change\n")

        diff = sandbox.capture_diff(handle)

        assert not os.path.exists(pager_marker)
        assert not os.path.exists(hook_marker)
        assert "src/app.py" in set(diff.paths)
    finally:
        sandbox.destroy(handle)


def test_capture_diff_reports_the_exact_unquoted_path_for_forbidden_globs(repo_mirror, sandbox_image, policy):
    """A non-ASCII, space-containing path under a forbidden glob must classify as AC8
    (red team R-A2): git's default quoting would otherwise hide it from a naive match."""
    sandbox = DockerSandbox(image=sandbox_image, repos_root=repo_mirror["repos_root"],
                             sandbox_root=repo_mirror["sandbox_root"])
    handle = sandbox.create(repo_mirror["repo"], repo_mirror["base_commit"])
    try:
        workflows_dir = os.path.join(handle.workdir, ".github", "workflows")
        os.makedirs(workflows_dir, exist_ok=True)
        tricky_name = "évil build.yml"
        with open(os.path.join(workflows_dir, tricky_name), "w") as f:
            f.write("name: evil\n")

        diff = sandbox.capture_diff(handle)

        expected_path = f".github/workflows/{tricky_name}"
        assert expected_path in diff.paths
        classification = classify(diff.changes, policy.floor)
        assert classification.action_class == "AC8"
    finally:
        sandbox.destroy(handle)
