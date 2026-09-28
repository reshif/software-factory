"""Lifecycle and adversarial evidence regressions for the Python package port."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys

import pytest

from software_factory.checks import verify_mission
from software_factory.core import (
    FactoryError,
    asset_root,
    hash_file,
    now,
    read_json,
    write_json,
)
from software_factory.evidence import fingerprint
from software_factory.workflow import (
    accept_scope,
    add_task,
    assess_current,
    assess_gate,
    assess_merged,
    create_mission,
    create_packet,
    edit_task,
    load_mission,
    load_result,
    mission_status,
    record_ci,
    record_decision,
    record_delivery,
    record_result,
    record_results,
    record_review,
    recover_lock,
    resume_mission,
    state_lock,
    transition_mission,
    transition_task,
)


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def commit(root, message="fixture candidate"):
    git(root, "add", ".")
    git(
        root,
        "-c",
        "user.name=Factory Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "-qm",
        message,
    )
    return git(root, "rev-parse", "HEAD")


def make_repo(
    root,
    code="print('check passed')",
    *,
    repair_attempts=1,
    delivery=False,
    setup=None,
    extra_ignore="",
):
    root.mkdir(exist_ok=True)
    (root / ".factory").mkdir()
    for name in ("CONSTITUTION.md", "workflow.json", "policy.json"):
        shutil.copyfile(asset_root() / name, root / ".factory" / name)
    (root / ".gitignore").write_text(".factory/local/\n.venv/\n__pycache__/\n" + extra_ignore)
    (root / "src").mkdir()
    (root / "src/app.py").write_text("VALUE = 1\n")
    config = {
        "schema_version": 1,
        "name": "fixture",
        "profile": "codex",
        "completion_target": "READY_PR",
        "work_types": ["feature", "patch", "maintenance"],
        "limits": {
            "repair_attempts": repair_attempts,
            "parallel_writers": 1,
            "check_timeout_seconds": 5,
        },
        "checks": [
            {
                "id": "unit",
                "command": [sys.executable, "-c", code],
                "cwd": ".",
                "required": True,
                "timeout_seconds": 5,
            }
        ],
        "owners": {"maintainer": "implementer", "reviewer": "reviewer"},
        "delivery": {
            "enabled": delivery,
            "staging_command": None,
            "release_command": None,
            "recovery_command": None,
        },
        "evidence_exclude": [
            ".factory/missions/",
            ".factory/local/",
            "factory.lock.json",
        ],
    }
    if setup:
        config["setup"] = setup
    write_json(root, "factory.json", config)
    git(root, "init", "-q", "-b", "main")
    commit(root, "baseline")
    return root


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "project")


def begin(root, id="M-ONE", kind="feature", owned=None):
    create_mission(root, {"id": id, "title": "Implement fixture change", "kind": kind})
    path = root / ".factory/missions" / id
    (path / "spec.md").write_text(
        "# Specification\n\nChange VALUE to 2.\n\n## Risks\n\nNo external services or persistent data.\n"
    )
    (path / "plan.md").write_text(
        "# Plan\n\nChange app value and verify.\n\n## Risks\n\nA caller may rely on the old constant.\n"
    )
    (path / "recovery.md").write_text("# Recovery\n\nRevert the constant; no migration is involved.\n")
    record_decision(
        root,
        id,
        {
            "id": "D-SCOPE",
            "kind": "scope",
            "reference": "User authorized fixture specification",
            "subject_hash": hash_file(root, f".factory/missions/{id}/spec.md"),
        },
    )
    accept_scope(root, id)
    add_task(
        root,
        id,
        {
            "id": "T-ONE",
            "title": "Change app",
            "owned_paths": owned or ["src/**"],
            "checks": ["unit"],
        },
    )
    transition_mission(root, id, "IMPLEMENTING")
    transition_task(root, id, "T-ONE", "RUNNING")
    return id


def result_for(root, id, verification, changed=None, task="T-ONE"):
    mission = load_mission(root, id)
    return {
        "schema_version": 1,
        "mission_id": id,
        "task_id": task,
        "fingerprint": fingerprint(root, mission)["fingerprint"],
        "status": "complete",
        "summary": "Fixture behavior is implemented and checked.",
        "changed_files": changed or ["src/app.py"],
        "checks": ["unit"],
        "evidence": [verification["reference"]],
        "unresolved": [],
        "created_at": now(),
    }


def finish(root, id, revision="R-ONE", changed=None):
    verified = complete(root, id, revision, changed)
    gate = assess_gate(root, id)
    assert gate["pass"], gate
    transition_mission(root, id, "READY_PR")
    return verified


def complete(root, id, revision="R-ONE", changed=None):
    """Drive the mission to a reviewed REVIEWING candidate without asserting readiness."""
    transition_task(root, id, "T-ONE", "VERIFYING")
    transition_mission(root, id, "VERIFYING")
    verified = verify_mission(root, id, revision)
    assert verified["pass"], verified
    record_result(root, id, result_for(root, id, verified, changed))
    transition_task(root, id, "T-ONE", "DONE")
    transition_mission(root, id, "REVIEWING")
    record_review(
        root,
        id,
        {
            "id": "V-" + revision,
            "author": "independent-reviewer",
            "status": "pass",
            "fingerprint": verified["fingerprint"],
            "findings": [],
        },
    )
    return verified


def test_full_lifecycle_binds_current_checks_results_review_and_packet(repo):
    id = begin(repo)
    (repo / "src/app.py").write_text("VALUE = 2\n")
    verified = finish(repo, id)
    assert load_result(repo, id, "T-ONE")["execution_attempt"] == 1
    assert mission_status(repo, id)["live_gate"]["pass"]
    packet = create_packet(repo, id)
    text = (repo / packet["path"]).read_text()
    assert "external CI" in text and verified["fingerprint"] in text
    assert (repo / ".factory/missions/M-ONE/recovery.md").read_text().startswith("# Recovery")
    (repo / "src/app.py").write_text("VALUE = 3\n")
    stale = mission_status(repo, id)
    assert stale["live_gate"]["stale"]
    assert any("stale" in reason for reason in stale["live_gate"]["reasons"])
    with pytest.raises(FactoryError, match="Cannot prepare ready PR"):
        create_packet(repo, id)


def test_scope_and_protected_controls_cannot_be_silently_changed(repo):
    id = begin(repo, owned=["src/**", "factory.json"])
    config = read_json(repo, "factory.json")
    config["checks"][0]["command"] = [sys.executable, "-c", "pass"]
    write_json(repo, "factory.json", config)
    gate = assess_gate(repo, id)
    assert any("Protected factory path" in reason for reason in gate["reasons"])
    (repo / ".factory/missions/M-ONE/spec.md").write_text("Different scope")
    with pytest.raises(FactoryError, match="Specification changed"):
        transition_task(repo, id, "T-ONE", "VERIFYING")


def test_hold_restore_budget_and_one_material_replan(repo):
    id = begin(repo)
    transition_mission(repo, id, "PAUSED", reason="User requested a pause")
    mission = load_mission(repo, id)
    assert mission["suspended_tasks"] == [{"id": "T-ONE", "status": "RUNNING"}]
    with pytest.raises(FactoryError, match="resolution"):
        resume_mission(repo, id, "IMPLEMENTING")
    resume_mission(repo, id, "IMPLEMENTING", resolution="Workspace reconciled")
    assert load_mission(repo, id)["tasks"][0]["attempts"] == 1
    transition_task(repo, id, "T-ONE", "BLOCKED")
    transition_task(repo, id, "T-ONE", "RUNNING")
    transition_task(repo, id, "T-ONE", "BLOCKED")
    with pytest.raises(FactoryError, match="now BLOCKED"):
        transition_task(repo, id, "T-ONE", "RUNNING")
    with pytest.raises(FactoryError, match="still exhausted"):
        resume_mission(repo, id, "IMPLEMENTING", resolution="Try again")
    edit_task(
        repo,
        id,
        "T-ONE",
        {
            "owned_paths": ["src/app.py"],
            "reason": "Diagnosed broad task; restrict the implementation to app.py",
        },
    )
    resume_mission(repo, id, "IMPLEMENTING", resolution="Accepted new task contract")
    transition_task(repo, id, "T-ONE", "RUNNING")
    task = load_mission(repo, id)["tasks"][0]
    assert task["attempts"] == 3 and task["attempt_base"] == 2 and task["budget_resets"] == 1
    transition_task(repo, id, "T-ONE", "BLOCKED")
    with pytest.raises(FactoryError, match="cycle"):
        edit_task(
            repo,
            id,
            "T-ONE",
            {"depends_on": ["T-ONE"], "reason": "Invalid self dependency"},
        )


def test_single_writer_across_missions_and_state_lock(repo):
    begin(repo)
    create_mission(repo, {"id": "M-TWO", "title": "Second mission"})
    with pytest.raises(FactoryError, match="State is locked"), state_lock(repo):
        record_decision(
            repo,
            "M-TWO",
            {
                "id": "D-SCOPE",
                "kind": "scope",
                "reference": "Authorized",
                "subject_hash": "a" * 64,
            },
        )
    with state_lock(repo), pytest.raises(FactoryError, match="still running"):
        recover_lock(repo)
    (repo / ".factory/missions/M-TWO/spec.md").write_text("Second task")
    record_decision(
        repo,
        "M-TWO",
        {
            "id": "D-SCOPE",
            "kind": "scope",
            "reference": "Authorized",
            "subject_hash": hash_file(repo, ".factory/missions/M-TWO/spec.md"),
        },
    )
    accept_scope(repo, "M-TWO")
    add_task(
        repo,
        "M-TWO",
        {"id": "T-TWO", "title": "Second writer", "owned_paths": ["src/**"]},
    )
    transition_mission(repo, "M-TWO", "IMPLEMENTING")
    with pytest.raises(FactoryError, match="occupied by mission"):
        transition_task(repo, "M-TWO", "T-TWO", "RUNNING")


def test_latest_rejection_and_tampered_log_block_readiness(repo):
    id = begin(repo)
    (repo / "src/app.py").write_text("VALUE = 2\n")
    evidence = finish(repo, id)
    record_review(
        repo,
        id,
        {
            "id": "V-REJECT",
            "author": "other-reviewer",
            "status": "changes_requested",
            "fingerprint": evidence["fingerprint"],
            "findings": [
                {
                    "id": "F-VALUE",
                    "severity": "blocking",
                    "path": "src/app.py",
                    "message": "The value is wrong for callers.",
                }
            ],
        },
    )
    assert not assess_gate(repo, id)["pass"]
    stdout = repo / evidence["checks"][0]["stdout_log"]["path"]
    stdout.write_text("tampered")
    gate = assess_gate(repo, id)
    assert any("log does not match" in reason for reason in gate["reasons"])


def test_results_batch_is_atomic_and_detects_record_tampering(repo):
    id = begin(repo)
    (repo / "src/app.py").write_text("VALUE = 2\n")
    evidence = verify_mission(repo, id, "R-ONE")
    record = result_for(repo, id, evidence)
    with pytest.raises(FactoryError, match="duplicate task"):
        record_results(repo, id, [record, record])
    assert not (repo / ".factory/missions/M-ONE/results/index.json").exists()
    published = record_result(repo, id, record)
    (repo / published["recorded"]).write_text("{}")
    with pytest.raises(FactoryError, match="hash mismatch"):
        load_result(repo, id, "T-ONE")


def test_ci_actual_fast_forward_merge_and_delivery(tmp_path):
    root = make_repo(tmp_path / "delivery", delivery=True)
    git(root, "switch", "-qc", "feature")
    id = begin(root)
    (root / "src/app.py").write_text("VALUE = 2\n")
    head = commit(root)
    evidence = finish(root, id)
    record_ci(
        root,
        id,
        url="https://ci.example.invalid/run/1",
        head=head,
        conclusion="success",
        trunk="main",
    )
    record_decision(
        root,
        id,
        {
            "id": "D-MERGE",
            "kind": "merge",
            "reference": "External review accepted fixture",
            "subject_hash": evidence["fingerprint"],
        },
    )
    record_delivery(root, id, {"merge_ref": head})
    assert not assess_merged(root, id)["pass"]
    git(root, "branch", "-f", "main", head)
    transition_mission(root, id, "MERGED")
    with pytest.raises(FactoryError, match="immutable"):
        record_delivery(root, id, {"merge_ref": load_mission(root, id)["base_commit"]})
    artifact = "sha256:" + "d" * 64
    record_delivery(root, id, {"artifact_digest": artifact})
    transition_mission(root, id, "STAGING")
    record_delivery(root, id, {"staging_ref": "https://ci.example.invalid/staging/1"})
    transition_mission(root, id, "AWAITING_RELEASE")
    record_delivery(
        root,
        id,
        {
            "release_ref": "External release decision",
            "recovery_ref": "External recovery procedure",
        },
    )
    with pytest.raises(FactoryError, match="exact artifact"):
        transition_mission(root, id, "DEPLOYING")
    record_decision(
        root,
        id,
        {
            "id": "D-RELEASE",
            "kind": "release",
            "reference": "External release decision",
            "subject_hash": "d" * 64,
        },
    )
    transition_mission(root, id, "DEPLOYING")
    record_delivery(root, id, {"deployment_ref": "https://ci.example.invalid/deploy/1"})
    transition_mission(root, id, "OBSERVING")
    record_delivery(root, id, {"observation": {"ref": "External metrics", "status": "healthy"}})
    transition_mission(root, id, "DELIVERED")
    assert assess_current(root, id)["pass"]
    with pytest.raises(FactoryError, match="immutable"):
        record_delivery(root, id, {"observation": {"ref": "Changed metrics", "status": "unhealthy"}})


def test_ignored_instructions_are_bound_without_false_initial_scope(tmp_path):
    root = make_repo(tmp_path / "hidden", extra_ignore="private/\n")
    (root / "private").mkdir()
    (root / "private/AGENTS.md").write_text("Existing local rules")
    id = begin(root)
    mission = load_mission(root, id)
    before = fingerprint(root, mission)
    assert "private/AGENTS.md" not in before["changed_paths"]
    (root / "private/AGENTS.md").write_text("Changed local rules")
    after = fingerprint(root, mission)
    assert before["fingerprint"] != after["fingerprint"]
    assert "private/AGENTS.md" in after["changed_paths"]


def test_late_monitor_mutation_prevents_gate_and_ready_publication(repo, monkeypatch):
    from test_checks import inject_mutation_at_monitor_close

    id = begin(repo)
    (repo / "src/app.py").write_text("VALUE = 2\n")
    finish(repo, id)
    transition_mission(repo, id, "IMPLEMENTING")
    transition_mission(repo, id, "VERIFYING")
    transition_mission(repo, id, "REVIEWING")
    inject_mutation_at_monitor_close(monkeypatch)
    gate = assess_gate(repo, id)
    assert not gate["pass"]
    assert any("changed during readiness" in reason for reason in gate["reasons"])
    with pytest.raises(FactoryError, match="Readiness gate failed"):
        transition_mission(repo, id, "READY_PR")
    assert load_mission(repo, id)["state"] == "REVIEWING"
    with pytest.raises(FactoryError, match="changed during readiness"):
        create_packet(repo, id)
    assert not (repo / ".factory/missions/M-ONE/pull-request.md").exists()


def test_late_monitor_mutation_blocks_task_completion_and_result_publication(repo, monkeypatch):
    from test_checks import inject_mutation_at_monitor_close

    id = begin(repo)
    (repo / "src/app.py").write_text("VALUE = 2\n")
    evidence = verify_mission(repo, id, "R-ONE")
    result = result_for(repo, id, evidence)
    record_result(repo, id, result)
    transition_task(repo, id, "T-ONE", "VERIFYING")
    original = load_mission(repo, id)
    inject_mutation_at_monitor_close(monkeypatch)
    with pytest.raises(FactoryError, match="changed during readiness"):
        transition_task(repo, id, "T-ONE", "DONE")
    assert load_mission(repo, id) == original
    index = read_json(repo, ".factory/missions/M-ONE/results/index.json")
    with pytest.raises(FactoryError, match="changed during result"):
        record_result(repo, id, result)
    assert read_json(repo, ".factory/missions/M-ONE/results/index.json") == index


@pytest.mark.parametrize("relative", ["private/AGENTS.md", ".factory/run.py", ".factory/docs/new.md"])
def test_new_ignored_governance_is_discovered_after_mission_creation(tmp_path, relative):
    root = make_repo(tmp_path / "hidden", extra_ignore="private/\n.factory/run.py\n.factory/docs/\n")
    id = begin(root)
    mission = load_mission(root, id)
    before = fingerprint(root, mission)
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("New governing instruction or launcher")
    after = fingerprint(root, mission)
    assert before["fingerprint"] != after["fingerprint"]
    assert relative in after["source_paths"] and relative in after["changed_paths"]
    assert any("Protected factory path" in reason for reason in assess_gate(root, id)["reasons"])


def test_unrecognized_file_under_mission_records_is_candidate_content(repo):
    from software_factory.evidence import is_metadata, is_mission_record_event

    id = begin(repo)
    (repo / "src/app.py").write_text("VALUE = 2\n")
    verified = finish(repo, id)
    assert is_metadata(".factory/missions/M-ONE/mission.json")
    assert is_metadata(verified["reference"])
    assert is_mission_record_event(".factory/missions/M-ONE/.mission.json.abcd_123", False)
    assert not is_metadata(".factory/missions/M-ONE/.mission.json.abcd_123")
    assert not is_metadata(".factory/missions/ZZZ/payload.py")
    payload = repo / ".factory/missions/ZZZ/payload.py"
    payload.parent.mkdir(parents=True)
    payload.write_text("print('hidden')\n")
    candidate = fingerprint(repo, load_mission(repo, id))
    assert ".factory/missions/ZZZ/payload.py" in candidate["changed_paths"]
    assert candidate["fingerprint"] != verified["fingerprint"]
    gate = assess_gate(repo, id)
    assert not gate["pass"]
    assert any("Unrecognized file in mission records" in reason for reason in gate["reasons"])
    # Even a task that owns the directory cannot make such a file a mission record.
    (repo / ".factory/missions/M-ONE/notes.py").write_text("x = 1\n")
    assert any("M-ONE/notes.py" in reason for reason in assess_gate(repo, id)["reasons"])


def weaken_unit_check(root):
    config = read_json(root, "factory.json")
    config["checks"][0]["command"] = [sys.executable, "-c", "pass"]
    write_json(root, "factory.json", config)


def test_maintenance_mission_cannot_silently_weaken_check_definitions(tmp_path):
    root = make_repo(
        tmp_path / "maint",
        code="import sys; sys.dont_write_bytecode = True; sys.path.insert(0, 'src'); import app; assert app.VALUE == 1",
    )
    id = begin(root, kind="maintenance", owned=["src/**", "factory.json"])
    (root / "src/app.py").write_text("VALUE = 2\n")  # breaks the baseline unit check
    weaken_unit_check(root)
    verified = complete(root, id, changed=["factory.json", "src/app.py"])
    gate = assess_gate(root, id)
    assert not gate["pass"]
    assert gate["check_changes"] and "unit command changed" in gate["check_changes"][0]
    assert any("Check definition weakened" in reason for reason in gate["reasons"])
    with pytest.raises(FactoryError, match="Check definition weakened"):
        transition_mission(root, id, "READY_PR")
    record_decision(
        root,
        id,
        {
            "id": "D-EXCEPTION",
            "kind": "exception",
            "reference": "Maintainer approved replacing the unit check",
            "subject_hash": verified["fingerprint"],
        },
    )
    gate = assess_gate(root, id)
    assert gate["pass"], gate
    transition_mission(root, id, "READY_PR")
    text = (root / create_packet(root, id)["path"]).read_text()
    assert "## Check definition changes" in text and "unit command changed" in text


@pytest.mark.parametrize("change", ["remove", "optional", "cwd"])
def test_check_definition_removal_optional_and_cwd_changes_are_detected(tmp_path, change):
    from software_factory.workflow import check_definition_changes

    root = make_repo(tmp_path / "maint")
    id = begin(root, kind="maintenance", owned=["src/**", "factory.json"])
    config = read_json(root, "factory.json")
    extra = {**config["checks"][0], "id": "extra", "required": False}
    config["checks"].append(extra)
    if change == "remove":
        config["checks"] = [extra]
    elif change == "optional":
        config["checks"][0]["required"] = False
    else:
        (root / "src/sub").mkdir()
        config["checks"][0]["cwd"] = "src/sub"
    changes = check_definition_changes(root, load_mission(root, id), config)
    assert (
        len(changes) == 1
        and {"remove": "removed", "optional": "optional", "cwd": "cwd"}[change] in changes[0]
    )


def test_reviews_decisions_and_records_respect_mission_state(repo):
    id = begin(repo)
    review = {
        "id": "V-EARLY",
        "author": "independent-reviewer",
        "status": "pass",
        "fingerprint": "a" * 64,
        "findings": [],
    }
    with pytest.raises(FactoryError, match="REVIEWING or READY_PR"):
        record_review(repo, id, review)
    create_mission(repo, {"id": "M-PROPOSED", "title": "Proposed mission"})
    with pytest.raises(FactoryError, match="REVIEWING or READY_PR"):
        record_review(repo, "M-PROPOSED", review)
    transition_mission(repo, "M-PROPOSED", "CANCELED", reason="No longer needed")
    decision = {"id": "D-LATE", "kind": "exception", "reference": "Late approval", "subject_hash": "b" * 64}
    with pytest.raises(FactoryError, match="terminal state CANCELED"):
        record_decision(repo, "M-PROPOSED", decision)
    with pytest.raises(FactoryError, match="terminal state CANCELED"):
        record_delivery(repo, "M-PROPOSED", {"pr_ref": "https://example.invalid/pr/1"})
    with pytest.raises(FactoryError, match="terminal state CANCELED"):
        record_review(repo, "M-PROPOSED", review)
    with pytest.raises(FactoryError, match="terminal state CANCELED"):
        add_task(repo, "M-PROPOSED", {"id": "T-LATE", "title": "Late task", "owned_paths": ["src/**"]})


def test_later_pass_must_resolve_earlier_blocking_findings(repo):
    id = begin(repo)
    (repo / "src/app.py").write_text("VALUE = 2\n")
    verified = finish(repo, id)
    blocking = {"severity": "blocking", "path": "src/app.py", "message": "Callers still expect 1."}
    rejection = {
        "id": "V-REJECT",
        "author": "other-reviewer",
        "status": "changes_requested",
        "fingerprint": verified["fingerprint"],
        "findings": [blocking],
    }
    with pytest.raises(FactoryError, match="needs an id"):
        record_review(repo, id, rejection)
    rejection["findings"] = [{**blocking, "id": "F-CALLERS"}]
    record_review(repo, id, rejection)
    later = {
        "id": "V-OVERRIDE",
        "author": "independent-reviewer",
        "status": "pass",
        "fingerprint": verified["fingerprint"],
        "findings": [],
    }
    record_review(repo, id, later)
    gate = assess_gate(repo, id)
    assert not gate["pass"]
    assert any("F-CALLERS" in reason and "no recorded resolution" in reason for reason in gate["reasons"])
    with pytest.raises(FactoryError, match="unresolved blocking finding"):
        record_review(
            repo, id, {**later, "id": "V-BAD", "resolutions": [{"finding": "F-OTHER", "reason": "n/a"}]}
        )
    with pytest.raises(FactoryError, match="resolution reason"):
        record_review(
            repo, id, {**later, "id": "V-EMPTY", "resolutions": [{"finding": "F-CALLERS", "reason": " "}]}
        )
    record_review(
        repo,
        id,
        {
            **later,
            "id": "V-RESOLVED",
            "resolutions": [{"finding": "F-CALLERS", "reason": "Callers audited; none use 1"}],
        },
    )
    assert assess_gate(repo, id)["pass"]
    assert "Resolved F-CALLERS" in (repo / create_packet(repo, id)["path"]).read_text()


def test_review_accepts_several_nonblocking_findings_without_ids(repo):
    id = begin(repo)
    (repo / "src/app.py").write_text("VALUE = 2\n")
    verified = finish(repo, id)
    note = {"severity": "nonblocking", "path": "src/app.py", "message": "Consider a docstring."}
    review = {
        "id": "V-NOTES",
        "author": "other-reviewer",
        "status": "pass",
        "fingerprint": verified["fingerprint"],
        "findings": [note, {**note, "message": "Consider a type hint."}, {**note, "id": "F-NOTE"}],
    }
    record_review(repo, id, review)
    assert assess_gate(repo, id)["pass"]
    with pytest.raises(FactoryError, match="Duplicate review finding id: F-NOTE"):
        record_review(repo, id, {**review, "id": "V-AGAIN", "findings": [{**note, "id": "F-NOTE"}]})


def test_historical_blocking_finding_without_id_needs_positional_resolution(repo):
    id = begin(repo)
    (repo / "src/app.py").write_text("VALUE = 2\n")
    verified = finish(repo, id)
    # A 0.2.0 review recorded blocking findings without ids.
    mission = read_json(repo, ".factory/missions/M-ONE/mission.json")
    note = {"severity": "nonblocking", "path": "src/app.py", "message": "Style."}
    blocking = {"severity": "blocking", "path": "src/app.py", "message": "Callers still expect 1."}
    mission["reviews"].append(
        {
            "id": "V-OLD",
            "author": "other-reviewer",
            "status": "changes_requested",
            "fingerprint": verified["fingerprint"],
            "findings": [note, blocking],
            "created_at": "2026-01-01T00:00:00Z",
        }
    )
    write_json(repo, ".factory/missions/M-ONE/mission.json", mission)
    later = {
        "id": "V-LATER",
        "author": "independent-reviewer",
        "status": "pass",
        "fingerprint": verified["fingerprint"],
        "findings": [],
    }
    record_review(repo, id, later)
    gate = assess_gate(repo, id)
    assert not gate["pass"]
    assert any("V-OLD-F2" in reason and "no recorded resolution" in reason for reason in gate["reasons"])
    with pytest.raises(FactoryError, match="Duplicate review finding id: V-OLD-F2"):
        record_review(repo, id, {**later, "id": "V-DUP", "findings": [{**blocking, "id": "V-OLD-F2"}]})
    with pytest.raises(FactoryError, match="unresolved blocking finding: V-OLD-F1"):
        record_review(
            repo, id, {**later, "id": "V-NB", "resolutions": [{"finding": "V-OLD-F1", "reason": "n/a"}]}
        )
    record_review(
        repo,
        id,
        {**later, "id": "V-RESOLVED", "resolutions": [{"finding": "V-OLD-F2", "reason": "Callers audited"}]},
    )
    assert assess_gate(repo, id)["pass"]


def test_evidence_selection_rejects_reordered_registration(repo):
    id = begin(repo)
    (repo / "src/app.py").write_text("VALUE = 2\n")
    finish(repo, id)
    transition_mission(repo, id, "IMPLEMENTING")
    transition_mission(repo, id, "VERIFYING")
    second = verify_mission(repo, id, "R-TWO")
    assert second["sequence"] == 2
    path = repo / ".factory/missions/M-ONE/mission.json"
    mission = read_json(repo, ".factory/missions/M-ONE/mission.json")
    mission["evidence"].reverse()
    write_json(repo, ".factory/missions/M-ONE/mission.json", mission)
    assert path.exists()
    gate = assess_gate(repo, id)
    assert any("registration order" in reason for reason in gate["reasons"])


def test_failing_verification_after_running_consumes_the_attempt(tmp_path):
    root = make_repo(
        tmp_path / "repair",
        code="import sys; sys.dont_write_bytecode = True; sys.path.insert(0, 'src'); import app; assert app.VALUE != 3",
    )
    id = begin(root)
    (root / "src/app.py").write_text("VALUE = 3\n")
    transition_task(root, id, "T-ONE", "VERIFYING")
    failed = verify_mission(root, id, "R-FAIL")
    assert not failed["pass"]
    assert load_mission(root, id)["tasks"][0]["repair_required"] == failed["reference"]
    (root / "src/app.py").write_text("VALUE = 2\n")  # repair without a new attempt
    passed = verify_mission(root, id, "R-PASS")
    assert passed["pass"]
    record_result(root, id, result_for(root, id, passed))
    with pytest.raises(FactoryError, match="spend a repair attempt"):
        transition_task(root, id, "T-ONE", "DONE")
    transition_task(root, id, "T-ONE", "RUNNING")
    task = load_mission(root, id)["tasks"][0]
    assert task["attempts"] == 2 and "repair_required" not in task
    transition_task(root, id, "T-ONE", "VERIFYING")
    transition_mission(root, id, "VERIFYING")
    verified = verify_mission(root, id, "R-AGAIN")
    record_result(root, id, result_for(root, id, verified))
    transition_task(root, id, "T-ONE", "DONE")
    # A later failure against a DONE task blocks readiness until it is reopened.
    (root / "src/app.py").write_text("VALUE = 3\n")
    verify_mission(root, id, "R-BROKEN")
    gate = assess_gate(root, id)
    assert any("reopen it through RUNNING" in reason for reason in gate["reasons"])


@pytest.mark.parametrize(("status", "consumes"), [("interrupted", False), ("timeout", True)])
def test_interrupted_verification_does_not_consume_the_attempt(tmp_path, monkeypatch, status, consumes):
    import software_factory.checks as checks_module

    root = make_repo(tmp_path / "repair")
    id = begin(root)
    transition_task(root, id, "T-ONE", "VERIFYING")
    original = checks_module.run_check

    def stopped(*args, **kwargs):
        # Ctrl-C/SIGTERM or the deadline stopped the check before it finished.
        return {**original(*args, **kwargs), "status": status, "exit_code": None}

    monkeypatch.setattr(checks_module, "run_check", stopped)
    result = verify_mission(root, id, "R-STOPPED")
    assert not result["pass"] and result["checks"][0]["status"] == status
    task = load_mission(root, id)["tasks"][0]
    assert task.get("repair_required") == (result["reference"] if consumes else None)


def test_reviewer_guidance_names_finding_ids_and_resolutions():
    for name in ("roles/reviewer.md", "templates/review.md", "skills/factory-review/SKILL.md"):
        text = (asset_root() / name).read_text()
        assert "`id`" in text and "resolutions" in text and '{"finding": ID, "reason": TEXT}' in text, name


def test_mission_input_is_root_relative(repo, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from software_factory.workflow import _mission_handler

    # 0.3.0: new missions need a request, so the JSON input names a root-relative request file.
    (repo / ".factory/local").mkdir(parents=True, exist_ok=True)
    (repo / ".factory/local/request.md").write_text("Create the input mission.\n")
    write_json(
        repo,
        ".factory/local/mission.json",
        {"id": "M-INPUT", "title": "From input", "request_file": ".factory/local/request.md"},
    )
    outside = tmp_path / "outside.json"
    outside.write_text('{"id": "M-OUTSIDE", "title": "Outside", "request_file": ".factory/local/request.md"}')
    args = SimpleNamespace(root=repo, mission_command="create", model_catalog=None, input=str(outside))
    with pytest.raises(FactoryError, match="Unsafe relative path"):
        _mission_handler(args)
    monkeypatch.chdir(tmp_path)
    args.input = ".factory/local/mission.json"
    assert _mission_handler(args)["id"] == "M-INPUT"


def test_trailing_slash_owned_path_covers_the_directory():
    from software_factory.evidence import matches_path

    assert matches_path("tests/test_app.py", "tests/")
    assert matches_path("tests/unit/deep/test_app.py", "tests/")
    assert not matches_path("tests", "tests/") and not matches_path("other/tests/x.py", "tests/")
    assert not matches_path("tests_extra/x.py", "tests/")
    with pytest.raises(FactoryError, match="Unsafe path pattern"):
        matches_path("x", "../tests/")


def test_transition_has_no_trunk_option():
    import argparse

    from software_factory.workflow import add_parser

    parser = argparse.ArgumentParser()
    add_parser(parser.add_subparsers(dest="command"))
    with pytest.raises(SystemExit):
        parser.parse_args(
            ["mission", "transition", "--mission", "M-ONE", "--to", "MERGED", "--trunk", "main"]
        )


def test_reason_and_next_are_rejected_for_non_hold_transitions(repo):
    create_mission(repo, {"id": "M-REASON", "title": "Reason flags", "kind": "feature"})
    before = load_mission(repo, "M-REASON")
    for flags, flag in (({"reason": "Planned it"}, "--reason"), ({"next": "Implement"}, "--next")):
        message = (
            f"{flag} is recorded only for PAUSED, BLOCKED or CANCELED transitions; "
            "omit it when moving to PLANNED"
        )
        with pytest.raises(FactoryError, match=re.escape(message)):
            transition_mission(repo, "M-REASON", "PLANNED", **flags)
    assert load_mission(repo, "M-REASON") == before
    transition_mission(repo, "M-REASON", "PAUSED", reason="Waiting for the user", next="Ask again")
    assert load_mission(repo, "M-REASON")["blockers"][-1]["reason"] == "Waiting for the user"


def test_task_input_rejects_unknown_and_managed_fields(repo):
    id = begin(repo)
    allowed = "id, title, depends_on, owned_paths, checks, criteria, model_assignment"
    task = {"id": "T-TWO", "title": "Second", "paths": ["src/**"], "checks": ["unit"]}
    with pytest.raises(FactoryError) as error:
        add_task(repo, id, task)
    assert str(error.value) == (
        f"Unknown task field 'paths' (did you mean owned_paths?); allowed fields: {allowed}"
    )
    with pytest.raises(FactoryError, match=r"Unknown task field 'status'; allowed fields: id, title"):
        add_task(repo, id, {"id": "T-TWO", "title": "Second", "owned_paths": ["src/**"], "status": "DONE"})
    assert [t["id"] for t in load_mission(repo, id)["tasks"]] == ["T-ONE"]
    with pytest.raises(FactoryError) as error:
        edit_task(repo, id, "T-ONE", {"paths": ["src/**"], "reason": "Narrow scope"})
    assert str(error.value) == (
        "Unknown task update field 'paths' (did you mean owned_paths?); allowed fields: "
        "title, depends_on, owned_paths, checks, criteria, model_assignment, reason"
    )
    with pytest.raises(FactoryError, match="Task identity, status and attempts cannot be edited: status"):
        edit_task(repo, id, "T-ONE", {"status": "DONE", "reason": "Skip"})
    with pytest.raises(FactoryError, match="Task update input must be a JSON object"):
        edit_task(repo, id, "T-ONE", ["title"])
