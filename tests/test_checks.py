"""Check subprocess bounds, private evidence, and candidate mutation regressions."""

import os
import sys
import time
import types

import pytest
from test_workflow import begin, commit, git, make_repo

from software_factory.checks import run_check, run_checks, verify_mission
from software_factory.core import FactoryError, read_json, write_json
from software_factory.evidence import candidate_snapshot


def test_required_checks_and_exact_dirty_candidate(tmp_path):
    root = make_repo(tmp_path / "product")
    good = run_checks(root, require_clean=True)
    assert good["pass"] and good["exact_revision"]
    (root / "src/app.py").write_text("VALUE = 2\n")
    dirty = run_checks(root)
    assert dirty["pass"] and not dirty["exact_revision"]
    assert dirty["dirty_paths"] == ["src/app.py"]
    with pytest.raises(FactoryError, match="clean committed candidate"):
        run_checks(root, require_clean=True)


@pytest.mark.parametrize(
    "code",
    [
        "from pathlib import Path; p=Path('src/app.py'); original=p.read_bytes(); p.write_text('mutated'); p.write_bytes(original)",
        "from pathlib import Path; p=Path('temporary-source.py'); p.write_text('transient'); p.unlink()",
        "from pathlib import Path; p=Path('.codex'); p.mkdir(); q=p/'config.toml'; q.write_text('changed'); q.unlink(); p.rmdir()",
    ],
)
def test_native_events_reject_transient_source_and_governance_mutations(tmp_path, code):
    root = make_repo(tmp_path / "product", code, extra_ignore=".codex/\n")
    result = run_checks(root, require_clean=True)
    assert not result["pass"]
    assert result["source_changed"] or result["monitoring_uncertain"]


def test_ignored_build_output_and_setup_are_allowed(tmp_path):
    setup = [
        {
            "id": "prepare",
            "command": [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('dist').mkdir(); Path('dist/generated').write_text('42')",
            ],
            "cwd": ".",
            "timeout_seconds": 5,
        }
    ]
    root = make_repo(
        tmp_path / "product",
        "from pathlib import Path; assert Path('dist/generated').read_text() == '42'",
        setup=setup,
        extra_ignore="dist/\n",
    )
    result = run_checks(root, require_clean=True)
    assert result["pass"], result
    assert result["setup"][0]["status"] == "pass"


def test_setup_failure_prevents_checks_and_records_logs(tmp_path):
    setup = [
        {
            "id": "unit",
            "command": [sys.executable, "-c", "raise SystemExit(7)"],
            "cwd": ".",
            "timeout_seconds": 5,
        }
    ]
    root = make_repo(tmp_path / "product", "raise RuntimeError('must not run')", setup=setup)
    id = begin(root)
    result = verify_mission(root, id, "R-FAIL")
    assert not result["pass"] and result["checks"] == []
    assert result["setup"][0]["exit_code"] == 7
    assert "/setup/unit.stderr.log" in result["setup"][0]["stderr_log"]["path"]
    assert (root / result["setup"][0]["stderr_log"]["path"]).stat().st_mode & 0o777 == 0o600


def test_output_limit_timeout_missing_program_and_no_shell(tmp_path):
    root = tmp_path
    output = run_check(
        root,
        {"id": "large", "command": [sys.executable, "-c", "print('x'*100000)"]},
        max_output_bytes=1024,
    )
    assert output["status"] == "output_limit" and output["truncated"]
    timeout = run_check(
        root,
        {
            "id": "slow",
            "command": [sys.executable, "-c", "import time; time.sleep(20)"],
        },
        default_timeout=0.1,
    )
    assert timeout["status"] == "timeout"
    missing = run_check(root, {"id": "missing", "command": ["this-program-does-not-exist-12345"]})
    assert missing["status"] == "error"
    literal = run_check(
        root,
        {
            "id": "argv",
            "command": [
                sys.executable,
                "-c",
                "import sys; print(sys.argv[1])",
                "$(touch should-not-exist)",
            ],
        },
    )
    assert literal["status"] == "pass" and not (root / "should-not-exist").exists()


def test_complete_large_logs_are_private_and_revision_never_overwrites(tmp_path):
    root = make_repo(tmp_path / "product", "print('x'*100000)")
    id = begin(root)
    result = verify_mission(root, id, "R-ONE")
    assert result["pass"] and not result["checks"][0]["truncated"]
    log = root / result["checks"][0]["stdout_log"]["path"]
    assert log.stat().st_size == 100001
    assert "x" * 100 not in (root / result["reference"]).read_text()
    with pytest.raises(FactoryError, match="already exists"):
        verify_mission(root, id, "R-ONE")


def test_private_logs_refuse_tracked_directory_and_local_only_ignore(tmp_path):
    root = make_repo(tmp_path / "product")
    id = begin(root)
    git(root, "add", "-f", ".factory/local")
    # state lock is removed after each operation; force a concrete private file.
    (root / ".factory/local/secret").write_text("secret")
    git(root, "add", "-f", ".factory/local/secret")
    with pytest.raises(FactoryError, match="tracked"):
        verify_mission(root, id, "R-ONE")


def test_index_flags_and_ignored_governance_do_not_hide_edits(tmp_path):
    root = make_repo(tmp_path / "product")
    git(root, "update-index", "--assume-unchanged", "src/app.py")
    (root / "src/app.py").write_text("VALUE = 9\n")
    with pytest.raises(FactoryError, match="assume-unchanged"):
        candidate_snapshot(root)
    git(root, "update-index", "--no-assume-unchanged", "src/app.py")
    assert "src/app.py" in candidate_snapshot(root)["dirty_paths"]
    os.symlink("app.py", root / "src/link.py")
    with pytest.raises(FactoryError, match="Symlink"):
        candidate_snapshot(root)


def test_optional_focused_check_failure_is_failure(tmp_path):
    root = make_repo(tmp_path / "product")
    config = read_json(root, "factory.json")
    config["checks"].append(
        {
            "id": "optional",
            "command": [sys.executable, "-c", "raise SystemExit(3)"],
            "cwd": ".",
            "required": False,
            "timeout_seconds": 5,
        }
    )
    write_json(root, "factory.json", config)
    commit(root)
    assert run_checks(root)["pass"]
    assert not run_checks(root, only="optional")["pass"]
    config["checks"][0]["required"] = False
    write_json(root, "factory.json", config)
    with pytest.raises(FactoryError, match="required"):
        run_checks(root)


def inject_mutation_at_monitor_close(monkeypatch):
    """Reproduce a native event delivered only during final observer teardown."""
    from software_factory.evidence import CandidateMonitor

    close = CandidateMonitor.close

    def late_close(self):
        if not self._closed:
            path = self.root / "src/app.py"
            content = path.read_bytes()
            path.write_text("transient final mutation")
            path.write_bytes(content)
        return close(self)

    monkeypatch.setattr(CandidateMonitor, "close", late_close)


def test_checks_consume_mutation_observed_only_at_final_close(tmp_path, monkeypatch):
    root = make_repo(tmp_path / "product")
    inject_mutation_at_monitor_close(monkeypatch)
    result = run_checks(root, require_clean=True)
    assert result["fingerprint"] == result["post_fingerprint"]
    assert not result["pass"] and result["source_changed"]
    assert not result["exact_revision"]


def test_verification_persists_final_monitor_mutation(tmp_path, monkeypatch):
    root = make_repo(tmp_path / "product")
    id = begin(root)
    inject_mutation_at_monitor_close(monkeypatch)
    result = verify_mission(root, id, "R-LATE")
    stored = read_json(root, result["reference"])
    assert result["fingerprint"] == result["post_fingerprint"]
    assert not result["pass"] and stored["source_changed"]


def test_monitor_shutdown_uncertainty_is_final_and_idempotent(tmp_path):
    from threading import Lock
    from types import SimpleNamespace

    from software_factory.evidence import CandidateMonitor

    monitor = object.__new__(CandidateMonitor)
    monitor.root = tmp_path
    monitor.known = set()
    monitor.metadata_prefixes = ()
    monitor.source_changed = False
    monitor.monitoring_reasons = []
    monitor._lock = Lock()
    monitor._pending = set()
    monitor._closed = False
    monitor.observer = SimpleNamespace(
        emitters=(),
        event_queue=SimpleNamespace(unfinished_tasks=0),
        is_alive=lambda: True,
        stop=lambda: None,
        join=lambda **_: None,
    )
    result = monitor.close()
    assert result["monitoring_uncertain"]
    assert "Filesystem observer did not stop" in result["monitoring_reasons"]
    assert monitor.close() == result


@pytest.mark.parametrize("operation", ["checks", "verify"])
def test_check_configuration_read_is_inside_observed_candidate(tmp_path, monkeypatch, operation):
    import software_factory.checks as checks_module

    root = make_repo(tmp_path / "product")
    mission_id = begin(root) if operation == "verify" else None
    original = checks_module.load_config

    def rewrite_after_loading(candidate_root):
        config = original(candidate_root)
        changed = read_json(candidate_root, "factory.json")
        changed["checks"][0]["command"] = [sys.executable, "-c", "raise SystemExit(7)"]
        write_json(candidate_root, "factory.json", changed)
        return config

    monkeypatch.setattr(checks_module, "load_config", rewrite_after_loading)
    result = (
        checks_module.run_checks(root)
        if operation == "checks"
        else checks_module.verify_mission(root, mission_id, "R-CONFIG")
    )
    assert result["checks"][0]["status"] == "pass"  # old command was loaded
    assert result["source_changed"] and not result["pass"]
    if operation == "verify":
        assert read_json(root, result["reference"])["source_changed"]


def test_transient_file_under_mission_records_is_a_source_change(tmp_path):
    code = (
        "from pathlib import Path; p=Path('.factory/missions/ZZZ/payload.py'); "
        "p.parent.mkdir(parents=True); p.write_text('x'); p.unlink(); p.parent.rmdir()"
    )
    root = make_repo(tmp_path / "product", code)
    result = run_checks(root, require_clean=True)
    assert not result["pass"] and result["source_changed"]


def test_inotify_queue_overflow_makes_monitoring_uncertain(tmp_path):
    import struct

    watchdog = pytest.importorskip("watchdog.observers.inotify_c")
    from software_factory.evidence import CandidateMonitor

    root = make_repo(tmp_path / "product")
    with CandidateMonitor(root) as monitor:
        assert not monitor.report()["monitoring_uncertain"]
        # The kernel reports IN_Q_OVERFLOW with wd -1; watchdog itself discards it.
        overflow = struct.pack("iIII", -1, watchdog.InotifyConstants.IN_Q_OVERFLOW, 0, 0)
        assert list(watchdog.Inotify._parse_event_buffer(overflow))
        report = monitor.close()
    assert report["monitoring_uncertain"]
    assert any("overflowed" in reason for reason in report["monitoring_reasons"])
    with CandidateMonitor(root) as later:
        assert not later.close()["monitoring_uncertain"]


def test_failed_directory_watch_makes_monitoring_uncertain(tmp_path):
    import ctypes
    import errno

    watchdog = pytest.importorskip("watchdog.observers.inotify_c")
    from software_factory.evidence import CandidateMonitor

    root = make_repo(tmp_path / "product")
    with CandidateMonitor(root) as monitor:
        ctypes.set_errno(errno.ENOSPC)
        with pytest.raises(OSError):
            watchdog.Inotify._raise_error()
        report = monitor.close()
    assert any("could not watch" in reason for reason in report["monitoring_reasons"])


class _BrokenInotifyModule(types.ModuleType):
    """Stand-in for watchdog.observers.inotify_c on a platform without inotify."""

    def __init__(self, error):
        super().__init__("watchdog.observers.inotify_c")
        self._error = error

    def __getattr__(self, name):
        raise self._error


def test_monitor_without_inotify_on_other_platforms_is_not_uncertain(tmp_path, monkeypatch):
    from watchdog.utils import UnsupportedLibcError

    from software_factory.evidence import CandidateMonitor

    root = make_repo(tmp_path / "product")
    # Importing the inotify backend on macOS raises UnsupportedLibcError; it must not be attempted.
    monkeypatch.setitem(
        sys.modules, "watchdog.observers.inotify_c", _BrokenInotifyModule(UnsupportedLibcError("libc"))
    )
    monkeypatch.setattr(sys, "platform", "darwin")
    with CandidateMonitor(root, source_paths={"src/app.py"}) as monitor:
        (root / "src/app.py").write_text("changed")
        deadline = time.monotonic() + 5
        while not monitor.report()["source_changed"] and time.monotonic() < deadline:
            time.sleep(0.05)
        report = monitor.close()
    assert report["source_changed"]
    assert not report["monitoring_uncertain"], report["monitoring_reasons"]


@pytest.mark.parametrize("error", ["unsupported-libc", "attribute", "type"])
def test_monitor_fails_closed_when_linux_loss_hooks_are_unavailable(tmp_path, monkeypatch, error):
    from watchdog.utils import UnsupportedLibcError

    from software_factory.evidence import CandidateMonitor

    root = make_repo(tmp_path / "product")
    if error == "unsupported-libc":
        module = _BrokenInotifyModule(UnsupportedLibcError("libc"))
    elif error == "type":
        module = _BrokenInotifyModule(TypeError("CDLL(None)"))
    else:
        module = types.ModuleType("watchdog.observers.inotify_c")
        module.Inotify = type("Inotify", (), {})  # lacks the private hooks
        module.InotifyConstants = types.SimpleNamespace(IN_Q_OVERFLOW=0x4000)
    monkeypatch.setitem(sys.modules, "watchdog.observers.inotify_c", module)
    monkeypatch.setattr(sys, "platform", "linux")
    with CandidateMonitor(root) as monitor:
        report = monitor.close()
    assert report["monitoring_uncertain"]
    assert any("event-loss detection is unavailable" in r for r in report["monitoring_reasons"])


def test_checks_do_not_receive_factory_inference_key(tmp_path, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "not-a-real-key")
    monkeypatch.setenv("SF_VISIBLE", "kept")
    result = run_check(
        tmp_path,
        {
            "id": "env",
            "command": [
                sys.executable,
                "-c",
                "import os; print(os.environ.get('TYPESAFE_API_KEY'), os.environ.get('SF_VISIBLE'))",
            ],
        },
    )
    assert result["stdout"].split() == ["None", "kept"]


def test_exited_check_with_background_child_is_labeled(tmp_path):
    code = "import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(5)'])"
    result = run_check(
        tmp_path, {"id": "orphan", "command": [sys.executable, "-c", code]}, default_timeout=0.5
    )
    assert result["status"] == "background_process" and result["exit_code"] == 0
