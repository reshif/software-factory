"""LocalSandbox and capture_diff, exercised against a real git repo in tmp_path.

LocalSandbox is explicitly not a security boundary (build spec §3 B3); these
tests only check the SandboxPort contract, not isolation. `capture_diff`
itself IS a security-relevant host operation though (it runs git on the
host), so the red-team regressions below (R-A1, R-A2) apply here exactly as
much as to DockerSandbox: both share `CheckoutSandbox`/`gitutils.build_diff`.
"""
import os

import pytest

from factory.policy.action_classes import classify
from factory.sandbox.local import LocalSandbox


def test_create_clones_and_checks_out_base_commit(repo_mirror):
    sandbox = LocalSandbox(repos_root=repo_mirror["repos_root"], sandbox_root=repo_mirror["sandbox_root"])
    handle = sandbox.create(repo_mirror["repo"], repo_mirror["base_commit"])

    assert handle.base_commit == repo_mirror["base_commit"]
    assert os.path.isdir(handle.workdir)
    assert (os.path.exists(os.path.join(handle.workdir, "README.md")))
    assert os.path.exists(os.path.join(handle.workdir, "src", "app.py"))


def test_create_raises_when_mirror_missing(tmp_path):
    sandbox = LocalSandbox(repos_root=str(tmp_path / "empty"), sandbox_root=str(tmp_path / "sandboxes"))
    with pytest.raises(FileNotFoundError):
        sandbox.create("nobody/nothing", "deadbeef")


def test_exec_runs_command_in_workdir(repo_mirror):
    sandbox = LocalSandbox(repos_root=repo_mirror["repos_root"], sandbox_root=repo_mirror["sandbox_root"])
    handle = sandbox.create(repo_mirror["repo"], repo_mirror["base_commit"])

    result = sandbox.exec(handle, ["cat", "README.md"])

    assert result.exit_code == 0
    assert result.stdout == "hello\n"


def test_exec_reports_nonzero_exit(repo_mirror):
    sandbox = LocalSandbox(repos_root=repo_mirror["repos_root"], sandbox_root=repo_mirror["sandbox_root"])
    handle = sandbox.create(repo_mirror["repo"], repo_mirror["base_commit"])

    result = sandbox.exec(handle, ["cat", "does-not-exist.txt"])

    assert result.exit_code != 0
    assert "does-not-exist.txt" in result.stderr


def test_exec_honors_timeout(repo_mirror):
    sandbox = LocalSandbox(repos_root=repo_mirror["repos_root"], sandbox_root=repo_mirror["sandbox_root"])
    handle = sandbox.create(repo_mirror["repo"], repo_mirror["base_commit"])

    result = sandbox.exec(handle, ["sleep", "5"], timeout_s=1)

    assert result.exit_code == 124


def test_capture_diff_reports_added_modified_and_deleted_files(repo_mirror):
    sandbox = LocalSandbox(repos_root=repo_mirror["repos_root"], sandbox_root=repo_mirror["sandbox_root"])
    handle = sandbox.create(repo_mirror["repo"], repo_mirror["base_commit"])

    # modify an existing file
    with open(os.path.join(handle.workdir, "src", "app.py"), "w") as f:
        f.write("def greet():\n    return 'hello there'\n")
    # add a new file
    os.makedirs(os.path.join(handle.workdir, "tests"), exist_ok=True)
    with open(os.path.join(handle.workdir, "tests", "test_app.py"), "w") as f:
        f.write("def test_greet():\n    assert True\n")
    # delete an existing file
    os.remove(os.path.join(handle.workdir, "to_delete.txt"))

    diff = sandbox.capture_diff(handle)

    by_path = {change.path: change for change in diff.changes}
    assert set(by_path) == {"src/app.py", "tests/test_app.py", "to_delete.txt"}

    assert by_path["src/app.py"].status == "modified"
    assert by_path["src/app.py"].added_lines >= 1
    assert by_path["src/app.py"].removed_lines >= 1
    assert "hello there" in by_path["src/app.py"].added_text

    assert by_path["tests/test_app.py"].status == "added"
    assert by_path["tests/test_app.py"].added_lines == 2
    assert by_path["tests/test_app.py"].removed_lines == 0
    assert "test_greet" in by_path["tests/test_app.py"].added_text

    assert by_path["to_delete.txt"].status == "deleted"
    assert by_path["to_delete.txt"].added_lines == 0
    assert by_path["to_delete.txt"].added_text == ""

    assert diff.base_commit == repo_mirror["base_commit"]
    assert "diff --git" in diff.patch
    assert diff.content_hash.startswith("sha256:")
    assert diff.lines == sum(c.added_lines + c.removed_lines for c in diff.changes)


def test_capture_diff_is_empty_when_nothing_changed(repo_mirror):
    sandbox = LocalSandbox(repos_root=repo_mirror["repos_root"], sandbox_root=repo_mirror["sandbox_root"])
    handle = sandbox.create(repo_mirror["repo"], repo_mirror["base_commit"])

    diff = sandbox.capture_diff(handle)

    assert diff.changes == ()
    assert diff.patch == ""


def test_destroy_removes_the_workdir(repo_mirror):
    sandbox = LocalSandbox(repos_root=repo_mirror["repos_root"], sandbox_root=repo_mirror["sandbox_root"])
    handle = sandbox.create(repo_mirror["repo"], repo_mirror["base_commit"])
    assert os.path.isdir(handle.workdir)

    sandbox.destroy(handle)

    assert not os.path.exists(handle.workdir)


def test_local_sandbox_warns_it_is_not_a_security_boundary(repo_mirror, caplog):
    with caplog.at_level("WARNING"):
        LocalSandbox(repos_root=repo_mirror["repos_root"], sandbox_root=repo_mirror["sandbox_root"])
    assert any("not a security boundary" in message.lower() for message in caplog.messages)


def test_git_dir_lives_outside_the_workdir_and_pointer_file_is_removed(repo_mirror):
    """create() uses --separate-git-dir; the workdir never keeps a `.git` (red team R-A1)."""
    sandbox = LocalSandbox(repos_root=repo_mirror["repos_root"], sandbox_root=repo_mirror["sandbox_root"])
    handle = sandbox.create(repo_mirror["repo"], repo_mirror["base_commit"])

    assert not os.path.exists(os.path.join(handle.workdir, ".git"))
    git_dir = sandbox._git_dir(handle.sandbox_id)
    assert os.path.isdir(git_dir)
    assert os.path.commonpath([git_dir, handle.workdir]) not in (git_dir, handle.workdir)


def test_capture_diff_ignores_a_hostile_git_config_planted_in_the_workdir(repo_mirror):
    """A `.git/config` and hooks an agent writes into the workdir must have zero effect
    on the host `git` invocation capture_diff runs (red team R-A1: sandbox->host RCE)."""
    sandbox = LocalSandbox(repos_root=repo_mirror["repos_root"], sandbox_root=repo_mirror["sandbox_root"])
    handle = sandbox.create(repo_mirror["repo"], repo_mirror["base_commit"])

    hostile_git_dir = os.path.join(handle.workdir, ".git")
    os.makedirs(hostile_git_dir)
    hooks_dir = os.path.join(handle.workdir, "evil-hooks")
    os.makedirs(hooks_dir)
    fsmonitor_marker = os.path.join(handle.workdir, "FSMONITOR_RAN")
    pager_marker = os.path.join(handle.workdir, "PAGER_RAN")
    hook_marker = os.path.join(handle.workdir, "HOOK_RAN")
    with open(os.path.join(hostile_git_dir, "config"), "w") as f:
        f.write(
            "[core]\n"
            f"\tfsmonitor = touch {fsmonitor_marker}\n"
            f"\thooksPath = {hooks_dir}\n"
            f"\tpager = touch {pager_marker}; cat\n"
        )
    hook_path = os.path.join(hooks_dir, "post-index-change")
    with open(hook_path, "w") as f:
        f.write(f"#!/bin/sh\ntouch {hook_marker}\n")
    os.chmod(hook_path, 0o755)

    with open(os.path.join(handle.workdir, "src", "app.py"), "a") as f:
        f.write("# a change\n")

    diff = sandbox.capture_diff(handle)

    assert not os.path.exists(fsmonitor_marker)
    assert not os.path.exists(pager_marker)
    assert not os.path.exists(hook_marker)
    # git never tracks a nested .git directory as content regardless of --git-dir, so
    # it's simply absent from the diff -- the point is that none of its commands ran.
    assert "src/app.py" in set(diff.paths)


def test_capture_diff_reports_the_exact_unquoted_path_for_forbidden_globs(repo_mirror, policy):
    """A non-ASCII, space-containing path under a forbidden glob must classify as AC8:
    git's default quoting would otherwise hide it from a naive string match (R-A2)."""
    sandbox = LocalSandbox(repos_root=repo_mirror["repos_root"], sandbox_root=repo_mirror["sandbox_root"])
    handle = sandbox.create(repo_mirror["repo"], repo_mirror["base_commit"])

    workflows_dir = os.path.join(handle.workdir, ".github", "workflows")
    os.makedirs(workflows_dir, exist_ok=True)
    tricky_name = "évil build.yml"  # non-ASCII + a space
    with open(os.path.join(workflows_dir, tricky_name), "w") as f:
        f.write("name: evil\n")

    diff = sandbox.capture_diff(handle)

    expected_path = f".github/workflows/{tricky_name}"
    assert expected_path in diff.paths

    classification = classify(diff.changes, policy.floor)
    assert classification.action_class == "AC8"
    assert any(expected_path in reason for reason in classification.reasons)
