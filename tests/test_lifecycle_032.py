"""0.3.2 lifecycle hardening: install/upgrade restore, dispatch trust, recovery and uninstall."""

import base64
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from software_factory.cli import _common_options, _dispatch, _requests_help, doctor, main
from software_factory.core import FactoryError, asset_path, asset_root, git, runtime_fingerprint, sha256
from software_factory.installation import ignore_plan, install, kernel_payload, uninstall
from software_factory.transactions import JOURNAL, LOCK, planning_snapshot, recover


def codes(root):
    return {issue["code"]: issue for issue in doctor(root)["issues"]}


# 1. H9: runtime-critical files are restored; other deletions stay deliberate.

RESTORED = (
    ".factory/run.py",
    ".factory/README.md",
    ".factory/src/software_factory/core.py",
    ".factory/schemas/factory.schema.json",
    ".factory/hooks/orchestrator_guard.py",
    ".factory/uv.lock",
)


@pytest.mark.parametrize("upgrade", [True, False])
def test_install_restores_deleted_runtime_critical_files(tmp_path, upgrade):
    install(tmp_path, selected="claude", skip_sync=True)
    payload = kernel_payload()
    for name in RESTORED:
        (tmp_path / name).unlink()
    (tmp_path / ".factory/docs/architecture.md").unlink()
    result = install(tmp_path, upgrade=upgrade, selected=None if upgrade else "claude", skip_sync=True)
    for name in RESTORED:
        assert name in result["changed"]
        assert (tmp_path / name).read_bytes() == payload[name]
    # A deleted document is a deliberate local change and stays deleted.
    assert not (tmp_path / ".factory/docs/architecture.md").exists()


def test_restore_after_upstream_change_is_not_a_conflict(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    (tmp_path / ".factory/run.py").unlink()
    payload = kernel_payload()
    payload[".factory/run.py"] += b"# new release\n"
    with patch("software_factory.installation.kernel_payload", return_value=payload):
        install(tmp_path, upgrade=True, skip_sync=True)
    assert (tmp_path / ".factory/run.py").read_bytes().endswith(b"# new release\n")


def test_missing_asset_message_names_the_real_fix(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    (tmp_path / ".factory/roles/orchestrator.md").unlink()
    with pytest.raises(FactoryError) as caught:
        asset_path(tmp_path, "roles/orchestrator.md")
    assert "git checkout -- .factory/roles/orchestrator.md" in str(caught.value)
    assert "upgrade keeps deleted assets deleted" in str(caught.value)
    (tmp_path / ".factory/schemas/mission.schema.json").unlink()
    with pytest.raises(FactoryError, match="upgrade \\(or init\\) restores it"):
        asset_path(tmp_path, "schemas/mission.schema.json")


def test_asset_path_refuses_corrupt_installation_json(tmp_path):
    (tmp_path / ".factory").mkdir()
    (tmp_path / ".factory/installation.json").write_text("{not json")
    with pytest.raises(FactoryError, match="Invalid .factory/installation.json"):
        asset_path(tmp_path, "schemas/mission.schema.json")
    (tmp_path / ".factory/installation.json").write_text("[]")
    with pytest.raises(FactoryError, match="Invalid .factory/installation.json"):
        asset_path(tmp_path, "schemas/mission.schema.json")


def test_runtime_pyproject_needs_no_readme():
    project = tomllib.loads((asset_root() / "runtime/pyproject.toml").read_text())["project"]
    assert "readme" not in project


# 2. H10: recover asks core.process_alive, which works on Windows too.


def test_recover_uses_process_alive(tmp_path):
    (tmp_path / LOCK).write_text("4242\n")
    with (
        patch("software_factory.transactions.process_alive", return_value=True) as alive,
        pytest.raises(FactoryError, match="4242 still exists"),
    ):
        recover(tmp_path)
    alive.assert_called_once_with(4242)
    with patch("software_factory.transactions.process_alive", return_value=False):
        result = recover(tmp_path, apply_recovery=True)
    # Captured before the stale lock was removed.
    assert result["stale_lock"] is True and not (tmp_path / LOCK).exists()


@pytest.mark.parametrize("content", ["", "abc\n", "0\n", "-5\n"])
def test_unreadable_lock_owner_fails_closed(tmp_path, content):
    (tmp_path / LOCK).write_text(content)
    with pytest.raises(FactoryError, match="Cannot establish"):
        recover(tmp_path, apply_recovery=True)
    assert (tmp_path / LOCK).exists()


# 10. Journal records are validated; the reported hash is of the file bytes.


def journal(root, files):
    path = root / JOURNAL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"schema_version": 1, "files": files}))
    return path


def b64(data):
    return base64.b64encode(data).decode()


@pytest.mark.parametrize(
    "record",
    [
        "not an object",
        {"before": 1, "after": None, "mode": 420},
        {"before": "!!not base64!!", "after": None, "mode": 420},
        {"before": None, "after": None},
        {"before": None, "after": None, "mode": "420"},
        {"before": None, "after": None, "mode": 0o7777},
    ],
)
def test_malformed_journal_record_is_a_factory_error(tmp_path, record):
    journal(tmp_path, {".factory/a": record})
    with pytest.raises(FactoryError, match="Invalid installation recovery journal"):
        recover(tmp_path)


def test_journal_hash_is_of_file_bytes(tmp_path):
    (tmp_path / ".factory").mkdir()
    (tmp_path / ".factory/a").write_bytes(b"new")
    path = journal(tmp_path, {".factory/a": {"before": b64(b"old"), "after": b64(b"new"), "mode": 420}})
    result = recover(tmp_path)
    assert result["journal_sha256"] == sha256(path.read_bytes())
    recover(tmp_path, apply_recovery=True)
    assert (tmp_path / ".factory/a").read_bytes() == b"old"


def test_non_object_journal_is_a_factory_error(tmp_path):
    journal(tmp_path, {})
    (tmp_path / JOURNAL).write_text("[1]")
    with pytest.raises(FactoryError, match="Invalid installation recovery journal"):
        recover(tmp_path)


# 3. C12: doctor/inspect never exec; drifted runtimes are refused.


def pinned_project(root):
    (root / ".factory/src/software_factory").mkdir(parents=True)
    (root / ".factory/src/software_factory/cli.py").write_text("")
    (root / ".factory/run.py").write_text("raise SystemExit(0)\n")
    files = {".factory/src/software_factory/cli.py": {"sha256": sha256(b""), "managed": True}}
    (root / ".factory/installation.json").write_text(
        json.dumps({"schema_version": 2, "runtime": "python-uv", "version": "0.0.1", "files": files})
    )
    bin_dir = root / ".factory/.venv/bin"
    bin_dir.mkdir(parents=True)
    os.symlink(sys.executable, bin_dir / "python")


@pytest.mark.skipif(os.name == "nt", reason="POSIX process replacement")
@pytest.mark.parametrize("command", ["doctor", "inspect"])
def test_doctor_and_inspect_never_exec_the_project_runtime(tmp_path, command):
    pinned_project(tmp_path)
    with (
        patch("software_factory.cli.os.execv", side_effect=AssertionError("exec")) as execv,
        patch("software_factory.cli.subprocess.run", side_effect=AssertionError("run")),
    ):
        assert _dispatch(tmp_path, command, [command]) is None
    execv.assert_not_called()


@pytest.mark.parametrize("pinned", [False, True])
def test_drifted_runtime_is_refused(tmp_path, pinned):
    pinned_project(tmp_path)
    (tmp_path / ".factory/src/software_factory/cli.py").write_text("print('tampered')\n")
    (tmp_path / ".factory/src/software_factory/extra.py").write_text("")
    with (
        patch("software_factory.cli._running_pinned", return_value=pinned),
        patch("software_factory.cli.os.execv", side_effect=AssertionError("exec")) as execv,
        pytest.raises(FactoryError) as caught,
    ):
        _dispatch(tmp_path, "status", ["status"])
    execv.assert_not_called()
    message = str(caught.value)
    assert ".factory/src/software_factory/cli.py" in message and "extra.py" in message
    assert "git checkout -- " in message and "software-factory upgrade" in message


def test_corrupt_manifest_refuses_dispatch(tmp_path):
    pinned_project(tmp_path)
    (tmp_path / ".factory/installation.json").write_text("{broken")
    with pytest.raises(FactoryError, match="Invalid .factory/installation.json"):
        _dispatch(tmp_path, "status", ["status"])


def test_inspect_help_says_read_only(capsys):
    with pytest.raises(SystemExit):
        main(["--help"])
    [line] = [line for line in capsys.readouterr().out.splitlines() if line.split()[:1] == ["inspect"]]
    assert "Read-only" in line


# 4-6. doctor reports each problem separately and gives the right advice.


def test_corrupt_installation_manifest_is_a_doctor_error(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    (tmp_path / ".factory/installation.json").write_text("{not json")
    report = doctor(tmp_path)
    issues = {issue["code"]: issue for issue in report["issues"]}
    assert issues["installation_manifest_invalid"]["severity"] == "error"
    assert not report["ok"]


def test_stale_exports_do_not_hide_other_doctor_checks(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    reviewer = tmp_path / ".claude/agents/factory-reviewer.md"
    reviewer.write_text(reviewer.read_text() + "local edit\n")
    report = doctor(tmp_path)
    issues = {issue["code"]: issue for issue in report["issues"]}
    assert issues["exports_stale"]["severity"] == "error" and report["exports"] == "stale"
    assert "configuration" not in issues
    assert {"configure_checks", "owner_unconfigured"} <= set(issues)


def test_uninitialised_project_is_told_to_init(tmp_path):
    message = codes(tmp_path)["runtime_missing"]["message"]
    assert "software-factory init" in message and "uv sync" not in message


# 7. Global option parsing.


def test_empty_root_is_an_error(capsys):
    with pytest.raises(SystemExit) as caught:
        main(["status", "--root="])
    assert caught.value.code == 1
    assert "--root requires a path" in json.loads(capsys.readouterr().err)["error"]


def test_root_scan_stops_at_double_dash():
    cleaned, root = _common_options(["checks", "--root", "a", "--", "--root", "b", "--root=c"])
    assert root == "a"
    assert cleaned == ["checks", "--", "--root", "b", "--root=c"]


def test_help_is_only_an_option():
    assert _requests_help(_common_options(["status", "--help"])[0])
    assert _requests_help(_common_options(["status", "-h"])[0])
    assert not _requests_help(_common_options(["status", "--root", "-h"])[0])
    assert not _requests_help(_common_options(["status", "--", "--help"])[0])
    assert not _requests_help(_common_options(["mission", "note", "--text=--help"])[0])


def test_version_ignores_a_bad_root(tmp_path, capsys):
    main(["version", "--root", str(tmp_path / "missing")])
    assert "version" in json.loads(capsys.readouterr().out)


# 8. Malformed records become JSON errors, not tracebacks.


@pytest.mark.parametrize(
    "error",
    [KeyError("task_id"), TypeError("unhashable type: 'list'"), AttributeError("'list' has no 'get'")],
)
def test_malformed_record_errors_are_reported_as_json(tmp_path, capsys, error):
    with (
        patch("software_factory.cli.inspect_project", side_effect=error),
        pytest.raises(SystemExit) as caught,
    ):
        main(["inspect", "--root", str(tmp_path)])
    assert caught.value.code == 1
    message = json.loads(capsys.readouterr().err)["error"]
    assert type(error).__name__ in message and str(error) in message


# 9. planning_snapshot tolerates user symlinks but not factory-owned ones.


def test_snapshot_skips_user_symlinks(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    (outside / "skill").mkdir(parents=True)
    (outside / "skill/SKILL.md").write_text("mine\n")
    (outside / "agent.md").write_text("mine\n")
    (tmp_path / ".claude/skills").mkdir(parents=True)
    (tmp_path / ".claude/agents").mkdir(parents=True)
    (tmp_path / ".claude/skills/personal").symlink_to(outside / "skill", target_is_directory=True)
    (tmp_path / ".claude/agents/personal.md").symlink_to(outside / "agent.md")
    snapshot = planning_snapshot(tmp_path)
    assert not any("personal" in name for name in snapshot)
    for owned in (".claude/skills/factory-build", ".claude/agents/factory-reviewer.md"):
        target = outside / "skill" if owned.endswith("build") else outside / "agent.md"
        (tmp_path / owned).symlink_to(target, target_is_directory=target.is_dir())
        with pytest.raises(FactoryError, match="Symlink"):
            planning_snapshot(tmp_path)
        (tmp_path / owned).unlink()
    (tmp_path / ".factory").mkdir()
    (tmp_path / ".factory/registry.json").symlink_to(outside / "agent.md")
    with pytest.raises(FactoryError, match="Symlink"):
        planning_snapshot(tmp_path)


# 11. The ignore block is replaced in place; git failures are not "exposed".

MARKER = "# software-factory private/runtime files"


def test_existing_ignore_block_is_replaced_not_duplicated(tmp_path):
    (tmp_path / ".gitignore").write_text(f"dist/\n{MARKER}\n.factory/local/\n.factory/.venv/\nuser-rule/\n")
    text = ignore_plan(tmp_path).decode()
    assert text.count(MARKER) == 1
    assert text == (
        f"dist/\n{MARKER}\n.factory/.venv/\n.factory/local/\n.factory-install.lock\n__pycache__/\nuser-rule/\n"
    )


def test_duplicate_ignore_blocks_collapse(tmp_path):
    block = f"{MARKER}\n.factory/.venv/\n.factory/local/\n.factory-install.lock\n__pycache__/\n"
    (tmp_path / ".gitignore").write_text(block + "user-rule/\n" + block)
    assert ignore_plan(tmp_path).decode() == block + "user-rule/\n"


def test_check_ignore_failure_is_an_error_not_exposure(tmp_path):
    real = subprocess.run

    def run(*args, **kwargs):
        if "check-ignore" in kwargs.get("args", []):
            return SimpleNamespace(returncode=128)
        return real(*args, **kwargs)

    with (
        patch("software_factory.installation.subprocess.run", side_effect=run),
        pytest.raises(FactoryError) as caught,
    ):
        ignore_plan(tmp_path)
    assert "check-ignore failed" in str(caught.value) and "expose" not in str(caught.value)


# 12. Only persisted-record schemas are history; missions need a mission.json.


def test_missions_placeholder_does_not_trigger_history_validation(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    (tmp_path / ".factory/missions").mkdir()
    (tmp_path / ".factory/missions/.gitkeep").write_text("")
    for name in (".factory/schemas/semantic.schema.json", ".factory/schemas/mission.schema.json"):
        data = (tmp_path / name).read_bytes() + b"\n"
        (tmp_path / name).write_bytes(data)
        manifest = json.loads((tmp_path / ".factory/installation.json").read_text())
        manifest["files"][name]["sha256"] = sha256(data)
        (tmp_path / ".factory/installation.json").write_text(json.dumps(manifest, indent=2) + "\n")
    planned = install(tmp_path, upgrade=True, skip_sync=True, dry_run=True)
    assert "history" not in planned


# 13. Staging timeouts give the exit-2 advice; git init only after a successful apply.


def test_staging_sync_timeout_is_exit_2_advice(tmp_path):
    with (
        patch("software_factory.installation.shutil.which", return_value="/usr/bin/uv"),
        patch(
            "software_factory.installation.subprocess.run",
            side_effect=subprocess.TimeoutExpired(["uv"], 180),
        ),
        pytest.raises(FactoryError) as caught,
    ):
        from software_factory.installation import _sync

        _sync(tmp_path)
    assert caught.value.exit_code == 2 and "timed out" in str(caught.value)
    assert "uv sync --locked --no-dev --project .factory" in str(caught.value)


def test_failed_apply_creates_no_repository(tmp_path):
    with (
        patch("software_factory.installation.apply", side_effect=FactoryError("injected")),
        pytest.raises(FactoryError, match="injected"),
    ):
        install(tmp_path, selected="claude", skip_sync=True, git_init=True)
    assert not (tmp_path / ".git").exists()
    install(tmp_path, selected="claude", skip_sync=True, git_init=True)
    assert (tmp_path / ".git").is_dir()


# 14. Uninstall removes directories it emptied and says what it keeps.


def test_uninstall_removes_emptied_directories_only(tmp_path):
    (tmp_path / ".claude/agents").mkdir(parents=True)
    (tmp_path / ".claude/agents/mine.md").write_text("user agent\n")
    (tmp_path / "empty-before").mkdir()
    install(tmp_path, selected="claude,codex,copilot", skip_sync=True)
    planned = uninstall(tmp_path, dry_run=True)
    assert (tmp_path / ".factory/roles").is_dir()
    result = uninstall(tmp_path)
    assert result["removed_directories"] == planned["removed_directories"]
    for directory in (".factory/roles", ".factory/src", ".claude/skills", ".agents", ".github/agents"):
        assert directory in result["removed_directories"]
        assert not (tmp_path / directory).exists()
    # Directories with user or retained content, and those never emptied by removal, stay.
    assert (tmp_path / ".claude/agents/mine.md").is_file()
    assert (tmp_path / "empty-before").is_dir() and (tmp_path / ".factory/local").is_dir()
    assert ".claude/agents" not in result["removed_directories"]
    assert ".factory/local/.gitignore" in result["retained"]
    assert any(MARKER in entry for entry in result["retained"])
    install(tmp_path, selected="claude,codex,copilot", skip_sync=True)


# 16. Runtime identity and git errors.


def test_fingerprint_refuses_dependency_without_file_record():
    with (
        patch("software_factory.core.importlib.metadata.distribution") as distribution,
        pytest.raises(FactoryError, match="no installed file record"),
    ):
        distribution.return_value = SimpleNamespace(requires=[], files=None)
        runtime_fingerprint()


def test_git_timeout_says_timed_out(tmp_path):
    with (
        patch("software_factory.core.subprocess.run", side_effect=subprocess.TimeoutExpired(["git"], 20)),
        pytest.raises(FactoryError) as caught,
    ):
        git(tmp_path, "status")
    assert "timed out" in str(caught.value) and "unavailable" not in str(caught.value)
    with (
        patch("software_factory.core.subprocess.run", side_effect=FileNotFoundError("git")),
        pytest.raises(FactoryError, match="Git unavailable"),
    ):
        git(Path(tmp_path), "status")
