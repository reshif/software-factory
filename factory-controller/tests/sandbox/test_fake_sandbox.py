"""FakeSandbox: an in-memory SandboxPort with no real git or Docker involved."""
import os

from factory.models import Diff, ExecResult
from factory.policy.action_classes import FileChange
from factory.sandbox.fake import FakeSandbox


def test_create_allocates_a_writable_workdir():
    sandbox = FakeSandbox()
    handle = sandbox.create("acme/widgets", "deadbeef")
    try:
        assert os.path.isdir(handle.workdir)
        assert handle.base_commit == "deadbeef"
        with open(os.path.join(handle.workdir, "x.txt"), "w") as f:
            f.write("hi\n")
        assert os.path.exists(os.path.join(handle.workdir, "x.txt"))
    finally:
        sandbox.destroy(handle)


def test_exec_returns_default_result_when_unscripted():
    sandbox = FakeSandbox()
    handle = sandbox.create("acme/widgets", "deadbeef")
    result = sandbox.exec(handle, ["pytest"])
    assert result.exit_code == 0
    sandbox.destroy(handle)


def test_exec_uses_the_scripted_callback():
    seen = {}

    def script(handle, cmd):
        seen["cmd"] = cmd
        return ExecResult(exit_code=1, stdout="", stderr="boom")

    sandbox = FakeSandbox(exec_script=script)
    handle = sandbox.create("acme/widgets", "deadbeef")
    result = sandbox.exec(handle, ["pytest", "-q"])
    assert result.exit_code == 1
    assert result.stderr == "boom"
    assert seen["cmd"] == ["pytest", "-q"]
    sandbox.destroy(handle)


def test_capture_diff_defaults_to_empty():
    sandbox = FakeSandbox()
    handle = sandbox.create("acme/widgets", "deadbeef")
    diff = sandbox.capture_diff(handle)
    assert diff.changes == ()
    assert diff.base_commit == "deadbeef"
    sandbox.destroy(handle)


def test_set_diff_scripts_capture_diff():
    sandbox = FakeSandbox()
    handle = sandbox.create("acme/widgets", "deadbeef")
    scripted_diff = Diff(base_commit="deadbeef",
                          changes=(FileChange("src/x.py", "added", 3, 0, "a\nb\nc"),),
                          patch="diff --git a/src/x.py b/src/x.py\n")
    sandbox.set_diff(handle.sandbox_id, scripted_diff)

    diff = sandbox.capture_diff(handle)

    assert diff == scripted_diff
    sandbox.destroy(handle)


def test_destroy_removes_the_workdir_and_forgets_the_handle():
    sandbox = FakeSandbox()
    handle = sandbox.create("acme/widgets", "deadbeef")
    assert os.path.isdir(handle.workdir)
    sandbox.destroy(handle)
    assert not os.path.exists(handle.workdir)
