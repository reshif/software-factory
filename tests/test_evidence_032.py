"""0.3.2 evidence and check hardening: hash-bound evidence, git-view binding, process cleanup."""

import hashlib
import json
import os
import shutil
import signal
import sys
import time

import pytest
from test_workflow import begin, commit, git, make_repo

import software_factory.checks as checks_module
from software_factory.checks import (
    fingerprint_change_reasons,
    run_check,
    run_checks,
    validate_verification,
    verify_mission,
)
from software_factory.core import FactoryError, load_config, process_alive, read_json, validate, write_json
from software_factory.evidence import candidate_diff, candidate_snapshot, fingerprint, matches_path
from software_factory.workflow import load_mission, update_mission

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX process groups, links and file names")
linux_only = pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux inotify and /proc")


def check_verification(root, mission_id):
    mission = load_mission(root, mission_id)
    return validate_verification(root, mission, load_config(root), fingerprint(root, mission))


def python_check(code):
    return [sys.executable, "-c", code]


# H1 / C4: evidence is bound to the hash recorded at registration.


def test_evidence_edited_after_registration_is_rejected(tmp_path):
    root = make_repo(tmp_path / "product", "raise SystemExit(1)")
    mission_id = begin(root)
    result = verify_mission(root, mission_id, "R-FAIL")
    assert not result["pass"]
    mission = load_mission(root, mission_id)
    assert mission["evidence_sha256"] == {
        result["reference"]: hashlib.sha256((root / result["reference"]).read_bytes()).hexdigest()
    }
    forged = read_json(root, result["reference"])
    forged["checks"][0].update({"status": "pass", "exit_code": 0})
    write_json(root, result["reference"], forged)
    reasons = check_verification(root, mission_id)["reasons"]
    assert any("Verification evidence changed after registration; run verify again" in r for r in reasons)


def test_evidence_without_registered_hash_is_rejected(tmp_path):
    root = make_repo(tmp_path / "product")
    mission_id = begin(root)
    assert verify_mission(root, mission_id, "R-ONE")["pass"]
    assert check_verification(root, mission_id)["reasons"] == []

    def unbind(mission):
        mission.pop("evidence_sha256")

    update_mission(root, mission_id, unbind)
    reasons = check_verification(root, mission_id)["reasons"]
    assert any("Verification evidence is not hash-bound; run verify again" in r for r in reasons)


def test_evidence_id_must_match_its_directory(tmp_path):
    root = make_repo(tmp_path / "product")
    mission_id = begin(root)
    first = verify_mission(root, mission_id, "R-ONE")
    moved = f".factory/missions/{mission_id}/evidence/R-TWO/checks.json"
    (root / moved).parent.mkdir()
    shutil.copyfile(root / first["reference"], root / moved)

    def swap(mission):
        mission["evidence"] = [moved]
        mission["evidence_sha256"] = {moved: mission["evidence_sha256"][first["reference"]]}

    update_mission(root, mission_id, swap)
    reasons = check_verification(root, mission_id)["reasons"]
    assert any("Evidence id does not match its registered directory" in r for r in reasons)


def test_unregistered_evidence_and_logs_are_removed(tmp_path, monkeypatch):
    root = make_repo(tmp_path / "product")
    mission_id = begin(root)
    original = checks_module._execute_suite

    def concurrent_change(*args, **kwargs):
        outcome = original(*args, **kwargs)

        def retitle(mission):
            mission["title"] = "Changed by a concurrent operation"

        update_mission(root, mission_id, retitle)
        return outcome

    monkeypatch.setattr(checks_module, "_execute_suite", concurrent_change)
    with pytest.raises(FactoryError, match="evidence was discarded"):
        verify_mission(root, mission_id, "R-RACE")
    assert not (root / f".factory/missions/{mission_id}/evidence/R-RACE").exists()
    assert not (root / f".factory/local/runs/{mission_id}/R-RACE").exists()
    assert load_mission(root, mission_id)["evidence"] == []
    monkeypatch.setattr(checks_module, "_execute_suite", original)
    assert verify_mission(root, mission_id, "R-RACE")["pass"]


def test_existing_private_logs_leave_no_evidence_directory(tmp_path):
    root = make_repo(tmp_path / "product")
    mission_id = begin(root)
    (root / f".factory/local/runs/{mission_id}/R-LOGS").mkdir(parents=True)
    with pytest.raises(FactoryError, match="Private logs already exist"):
        verify_mission(root, mission_id, "R-LOGS")
    assert not (root / f".factory/missions/{mission_id}/evidence/R-LOGS").exists()


# H6 / C10: inode snapshots and Git control files.


@posix_only
def test_hard_link_write_and_restore_through_git_directory_is_a_change(tmp_path):
    code = (
        "import os; from pathlib import Path; os.link('src/app.py', '.git/sf-link'); p=Path('.git/sf-link'); "
        "original=p.read_bytes(); p.write_text('mutated'); p.write_bytes(original); os.unlink('.git/sf-link')"
    )
    root = make_repo(tmp_path / "product", code)
    result = run_checks(root, require_clean=True)
    assert result["fingerprint"] == result["post_fingerprint"]
    assert not result["pass"] and result["source_changed"], result
    assert any("src/app.py" in r for r in result["monitoring_reasons"]), result["monitoring_reasons"]


@posix_only
def test_candidate_file_with_another_hard_link_is_uncertain(tmp_path):
    root = make_repo(tmp_path / "product")
    os.link(root / "src/app.py", tmp_path / "outside-link.py")
    result = run_checks(root)
    assert not result["pass"] and result["monitoring_uncertain"]
    assert any("more than one hard link" in r and "src/app.py" in r for r in result["monitoring_reasons"])


@linux_only
def test_check_rewriting_info_exclude_cannot_hide_a_new_file(tmp_path):
    code = (
        "from pathlib import Path; e=Path('.git/info/exclude'); e.parent.mkdir(exist_ok=True); "
        "e.write_text(e.read_text() + '\\nhidden.py\\n' if e.exists() else 'hidden.py\\n'); "
        "Path('hidden.py').write_text('x')"
    )
    root = make_repo(tmp_path / "product", code)
    result = run_checks(root)
    assert not result["pass"] and result["source_changed"], result
    assert result["fingerprint"] != result["post_fingerprint"]
    reasons = " ".join(result["monitoring_reasons"])
    assert ".git/info/exclude" in reasons
    assert "Git ignore, attribute, filter or textconv settings changed during checks" in reasons


@linux_only
def test_git_config_and_hook_writes_are_changes(tmp_path):
    code = (
        "from pathlib import Path; h=Path('.git/hooks/pre-commit'); h.write_text('#!/bin/sh'); h.unlink(); "
        "c=Path('.git/config'); original=c.read_bytes(); c.write_bytes(original + b'\\n'); c.write_bytes(original)"
    )
    root = make_repo(tmp_path / "product", code)
    result = run_checks(root)
    assert not result["pass"] and result["source_changed"]
    reason = next(r for r in result["monitoring_reasons"] if r.startswith("Candidate paths changed during"))
    assert ".git/config" in reason and ".git/hooks/pre-commit" in reason


# H7: a tracked path dropped from the index stays bound.


def test_path_dropped_from_index_and_ignored_stays_in_the_fingerprint(tmp_path):
    root = make_repo(tmp_path / "product")
    git(root, "rm", "-q", "--cached", "src/app.py")
    (root / ".git/info/exclude").write_text("src/app.py\n")
    before = candidate_snapshot(root)
    assert "src/app.py" in before["source_paths"]
    assert "src/app.py" in before["dirty_paths"] and "src/app.py" in before["changed_paths"]
    (root / "src/app.py").write_text("VALUE = 99\n")
    after = candidate_snapshot(root)
    assert after["fingerprint"] != before["fingerprint"]


# Filters and attributes cannot hide content.


def hide_with_clean_filter(root):
    (root / ".git/info/attributes").write_text("src/app.py filter=hide\n")
    command = f"{sys.executable} -c \"import sys; sys.stdin.read(); sys.stdout.write('VALUE = 1\\\\n')\""
    git(root, "config", "filter.hide.clean", command)


def test_clean_filter_cannot_hide_a_changed_file(tmp_path):
    root = make_repo(tmp_path / "product")
    clean = candidate_snapshot(root)
    hide_with_clean_filter(root)
    assert git(root, "diff", "--stat") == ""  # Git's own view is fooled
    filtered = candidate_snapshot(root)
    assert filtered["fingerprint"] != clean["fingerprint"]  # the Git view is bound
    (root / "src/app.py").write_text("VALUE = 2\n")
    changed = candidate_snapshot(root)
    assert changed["dirty_paths"] == ["src/app.py"]
    diff = candidate_diff(root, changed["head"], ["src/app.py"])
    assert diff["stats"]["src/app.py"] == (1, 1, False)
    assert b"+VALUE = 2" in diff["patch"]


def test_crlf_checkout_of_committed_lf_file_is_unchanged(tmp_path):
    root = make_repo(tmp_path / "product")
    (root / "src/app.py").write_bytes(b"VALUE = 1\r\n")
    assert candidate_snapshot(root)["dirty_paths"] == []


def test_diff_attribute_does_not_zero_line_counts(tmp_path):
    root = make_repo(tmp_path / "product")
    (root / ".git/info/attributes").write_text("*.py -diff\n")
    (root / "src/app.py").write_text("VALUE = 2\nOTHER = 3\n")
    diff = candidate_diff(root, git(root, "rev-parse", "HEAD"), ["src/app.py"])
    assert diff["stats"]["src/app.py"] == (2, 1, False)
    assert b"-VALUE = 1" in diff["patch"]
    assert any("src/app.py" in r and "binary" in r for r in diff["reasons"])


def test_added_file_binary_detection_matches_git(tmp_path):
    root = make_repo(tmp_path / "product")
    (root / "late-nul.txt").write_bytes(b"a\n" * 5000 + b"\0")
    (root / "early-nul.bin").write_bytes(b"\0abc")
    diff = candidate_diff(root, git(root, "rev-parse", "HEAD"), ["late-nul.txt", "early-nul.bin"])
    assert diff["stats"]["late-nul.txt"] == (5001, 0, False)
    assert diff["stats"]["early-nul.bin"] == (0, 0, True)


# Ignored writes are recorded.


def test_ignored_writes_are_recorded_in_evidence(tmp_path):
    code = "from pathlib import Path; Path('dist').mkdir(); Path('dist/generated').write_text('42')"
    root = make_repo(tmp_path / "product", code, extra_ignore="dist/\n")
    mission_id = begin(root)
    result = verify_mission(root, mission_id, "R-IGNORED")
    assert result["pass"], result
    stored = read_json(root, result["reference"])
    assert "dist/generated" in stored["ignored_writes"]
    assert stored["ignored_writes_total"] == len(stored["ignored_writes"])


# Non-UTF-8 names.


@linux_only
def test_non_utf8_candidate_path_is_refused_clearly(tmp_path):
    root = make_repo(tmp_path / "product")
    (root / os.fsdecode(b"bad\xff.py")).write_text("x")
    with pytest.raises(FactoryError, match="not valid UTF-8"):
        candidate_snapshot(root)


@linux_only
def test_non_utf8_event_path_is_a_named_change_not_a_crash(tmp_path):
    code = "import os; fd=os.open(b'bad\\xff.tmp', os.O_CREAT | os.O_WRONLY); os.close(fd); os.unlink(b'bad\\xff.tmp')"
    root = make_repo(tmp_path / "product", code)
    result = run_checks(root)
    assert not result["pass"] and result["source_changed"]
    json.dumps(result, ensure_ascii=False).encode("utf-8")
    assert any("bad\\xff.tmp" in r for r in result["monitoring_reasons"]), result["monitoring_reasons"]


# Symlinks, submodules and path patterns.


@posix_only
def test_symlink_in_candidate_names_the_limit(tmp_path):
    root = make_repo(tmp_path / "product")
    os.symlink("app.py", root / "src/link.py")
    with pytest.raises(FactoryError, match="Symlink in candidate: src/link.py.*not supported"):
        candidate_snapshot(root)


def test_submodule_names_the_limit(tmp_path):
    root = make_repo(tmp_path / "product")
    nested = make_repo(root / "nested")
    shutil.rmtree(nested / ".factory")
    git(root, "add", "nested")
    with pytest.raises(FactoryError, match="submodules and nested repositories are not supported"):
        candidate_snapshot(root)


def test_path_patterns_reject_dot_components():
    for pattern in ("./src/app.py", "src/./app.py", "src/."):
        with pytest.raises(FactoryError, match="Unsafe path pattern"):
            matches_path("src/app.py", pattern)
    assert matches_path("src/app.py", "src/")


# C11 / C2: program resolution and background processes.


@posix_only
def test_check_records_resolved_program_and_hash(tmp_path, monkeypatch):
    tools = tmp_path / "tools"
    tools.mkdir()
    tool = tools / "fake-tool"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tools}{os.pathsep}{os.environ['PATH']}")
    result = run_check(tmp_path, {"id": "tool", "command": ["fake-tool"]})
    assert result["status"] == "pass"
    assert result["resolved_program"] == str(tool)
    assert result["program_sha256"] == hashlib.sha256(tool.read_bytes()).hexdigest()
    missing = run_check(tmp_path, {"id": "missing", "command": ["no-such-program-12345"]})
    assert missing["resolved_program"] is None and missing["program_sha256"] is None


def wait_dead(pid):
    deadline = time.monotonic() + 3
    while process_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    return not process_alive(pid)


@posix_only
@pytest.mark.parametrize("new_session", [False, True])
def test_background_process_without_pipes_is_killed_and_labeled(tmp_path, new_session):
    if new_session and not sys.platform.startswith("linux"):
        pytest.skip("descendants outside the process group are found through /proc")
    code = (
        "import subprocess, sys; from pathlib import Path; "
        "p=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], "
        f"stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session={new_session}); "
        "Path('daemon.pid').write_text(str(p.pid))"
    )
    result = run_check(tmp_path, {"id": "daemon", "command": python_check(code)}, default_timeout=5)
    assert result["status"] == "background_process" and result["exit_code"] == 0
    assert "still running" in result["reason"]
    assert wait_dead(int((tmp_path / "daemon.pid").read_text()))


def test_clean_check_has_no_background_label(tmp_path):
    result = run_check(tmp_path, {"id": "quick", "command": python_check("print('ok')")})
    assert result["status"] == "pass" and "reason" not in result


# C4: SIGTERM handling.


@pytest.mark.skipif(os.name != "posix", reason="SIGTERM delivery")
def test_sigterm_before_start_does_not_run_the_check(tmp_path, monkeypatch):
    original_handler = signal.getsignal(signal.SIGTERM)
    real_signal, fired = signal.signal, []

    def deliver_immediately(number, handler):
        previous = real_signal(number, handler)
        if number == signal.SIGTERM and callable(handler) and not fired:
            fired.append(True)
            handler(number, None)
        return previous

    monkeypatch.setattr(signal, "signal", deliver_immediately)
    try:
        code = "from pathlib import Path; Path('ran').write_text('yes')"
        result = run_check(tmp_path, {"id": "late", "command": python_check(code)})
    finally:
        real_signal(signal.SIGTERM, original_handler)
    assert result["status"] == "interrupted"
    assert not (tmp_path / "ran").exists()


def test_unknown_previous_sigterm_handler_is_restored_to_default(tmp_path, monkeypatch):
    original_handler = signal.getsignal(signal.SIGTERM)
    monkeypatch.setattr(signal, "getsignal", lambda number: None)
    try:
        run_check(tmp_path, {"id": "quick", "command": python_check("pass")})
        monkeypatch.undo()
        assert signal.getsignal(signal.SIGTERM) is signal.SIG_DFL
    finally:
        signal.signal(signal.SIGTERM, original_handler)


# C5 / C7: limits and fingerprint attribution.


def test_per_check_output_limit_cannot_raise_the_global_limit(tmp_path):
    root = make_repo(tmp_path / "product", "print('x' * 100000)")
    config = read_json(root, "factory.json")
    config["limits"]["check_output_bytes"] = 1024
    config["checks"][0]["output_limit_bytes"] = 67108864
    write_json(root, "factory.json", config)
    commit(root)
    result = run_checks(root)
    assert result["checks"][0]["status"] == "output_limit" and not result["pass"]


def test_fingerprint_change_reasons_name_a_path_that_stayed_dirty(tmp_path):
    root = make_repo(tmp_path / "product")
    (root / "src/app.py").write_text("VALUE = 2\n")
    before = candidate_snapshot(root)
    (root / "src/app.py").write_text("VALUE = 3\n")
    after = candidate_snapshot(root)
    assert before["dirty_paths"] == after["dirty_paths"] == ["src/app.py"]
    reasons = fingerprint_change_reasons(before, after, {"source_changed": False})
    assert reasons == ["Candidate paths changed between the pre- and post-check fingerprints: src/app.py"]


# Schema.


def test_evidence_schema_requires_monitoring_fields_and_exact_ids(tmp_path):
    root = make_repo(tmp_path / "product")
    mission_id = begin(root)
    stored = read_json(root, verify_mission(root, mission_id, "R-ONE")["reference"])
    validate(root, "evidence", stored)
    for field in ("monitoring_uncertain", "monitoring_reasons", "fingerprint_format"):
        broken = {k: v for k, v in stored.items() if k != field}
        with pytest.raises(FactoryError):
            validate(root, "evidence", broken)
    for field, value in (("id", "1-run"), ("mission_id", "M" * 81), ("head", "a" * 50)):
        with pytest.raises(FactoryError):
            validate(root, "evidence", {**stored, field: value})
    broken = json.loads(json.dumps(stored))
    broken["checks"][0]["status"] = "skipped"
    with pytest.raises(FactoryError):
        validate(root, "evidence", broken)
