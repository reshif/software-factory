"""0.3.1 constitution 2.0.0: reconciling in-flight missions, upgrade reporting and instruction budget."""

from __future__ import annotations

import json
import re

import pytest
from test_mission_030 import cli, put
from test_workflow import begin, commit, make_repo

from software_factory.core import CONSTITUTION_PATH, FactoryError, asset_root, hash_file, sha256
from software_factory.installation import install
from software_factory.rendering import END, START
from software_factory.workflow import (
    accept_scope,
    assess_gate,
    constitution_version,
    load_mission,
    record_decision,
    transition_mission,
)

# Managed AGENTS.md block for a claude,codex,copilot install with constitution 2.0.0 measured
# 6227 bytes; the budget is that size + 10%. Growing past it needs a deliberate, reviewed change.
AGENTS_BLOCK_BUDGET = 6849
CONSTITUTION_WORD_LIMIT = 800


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "project")


def amend_constitution(root):
    path = root / CONSTITUTION_PATH
    text = path.read_text()
    amended = re.sub(r"^Version: \d+\.\d+\.\d+", "Version: 2.1.0", text, count=1, flags=re.MULTILINE)
    assert amended != text
    path.write_text(amended + "\n20. **Fixture amendment.** Added for a reconciliation test.\n")
    return hash_file(root, CONSTITUTION_PATH)


def implementing(root, kind="feature"):
    id = begin(root, kind=kind)  # IMPLEMENTING with T-ONE RUNNING
    mission = load_mission(root, id)
    assert mission["constitution_version"] == constitution_version(
        (asset_root() / "CONSTITUTION.md").read_text()
    )
    assert mission["tasks"][0]["attempts"] == 1
    return id


def exception(
    root, id, subject_hash, decision_id=None, reference="Maintainer approved constitution 2.1.0 in PR #7"
):
    return record_decision(
        root,
        id,
        {
            "id": decision_id or f"D-CONST-{subject_hash[:8]}",
            "kind": "exception",
            "subject_hash": subject_hash,
            "reference": reference,
        },
    )


def test_constitution_version_is_parsed_from_version_line():
    assert constitution_version("# C\n\nVersion: 2.0.0 · Ratified: 2026-09-28\n") == "2.0.0"
    assert constitution_version("# C\n\nNo version here\n") is None
    assert constitution_version((asset_root() / "CONSTITUTION.md").read_text()) == "2.0.0"


def test_product_mission_reconciles_changed_constitution(repo):
    id = implementing(repo)
    before = load_mission(repo, id)
    new_hash = amend_constitution(repo)

    with pytest.raises(FactoryError) as refused:
        transition_mission(repo, id, "VERIFYING")
    message = str(refused.value)
    assert message.startswith("Constitution changed (mission bound to sha256 " + before["constitution_hash"])
    assert f"{CONSTITUTION_PATH} is now sha256 {new_hash}, version 2.1.0" in message
    assert f"software-factory mission block --mission {id} --reason" in message
    # 0.3.2: the exception is the user's own approval, so the hint names `mission approve`.
    assert (
        f"software-factory mission approve --mission {id} --kind exception --subject-hash {new_hash} "
        f"--id D-CONST-{new_hash[:8]}"
    ) in message
    assert "mission decision" not in message
    assert f"software-factory mission accept-scope --mission {id}" in message
    payload = {"id": f"D-CONST-{new_hash[:8]}", "kind": "exception", "subject_hash": new_hash}
    gate = assess_gate(repo, id)
    assert not gate["pass"]
    assert any(
        r.startswith("Constitution changed since mission acceptance: ")
        and new_hash in r
        and "accept-scope" in r
        for r in gate["reasons"]
    )

    transition_mission(repo, id, "BLOCKED", reason="Constitution changed; reconcile before continuing")
    with pytest.raises(FactoryError, match=f"exact new constitution sha256 {new_hash}"):
        accept_scope(repo, id)  # no exception decision
    exception(repo, id, before["constitution_hash"], decision_id="D-CONST-OLD")
    exception(repo, id, "0" * 64, decision_id="D-CONST-WRONG")
    with pytest.raises(FactoryError, match="Constitution changes require an exception decision"):
        accept_scope(repo, id)  # decisions bound to the wrong hashes
    assert load_mission(repo, id)["constitution_hash"] == before["constitution_hash"]

    payload["reference"] = "Maintainer approved constitution 2.1.0 in PR #7"
    # 0.3.2: an exception is the user's own approval, recorded through `mission approve`.
    from software_factory.workflow import approve_decision

    approve_decision(
        repo, id, "exception", payload["reference"], payload["subject_hash"], payload["id"], lambda *_: None
    )
    # 0.3.2: a product mission's base must advance past the constitution commit, so it must exist.
    with pytest.raises(FactoryError, match="commit the constitution change first"):
        cli(repo, "mission", "accept-scope", "--mission", id)
    head = commit(repo, "Adopt constitution 2.1.0")
    mission = cli(repo, "mission", "accept-scope", "--mission", id)
    assert mission["state"] == "PLANNED" and mission["previous_state"] is None
    assert mission["base_commit"] == head
    assert mission["base_history"][0]["from"] == before["base_commit"]
    assert mission["base_history"][0]["decision"] == payload["id"]
    assert mission["constitution_hash"] == new_hash and mission["constitution_version"] == "2.1.0"
    assert [t["status"] for t in mission["tasks"]] == ["TODO"]
    assert mission["tasks"][0]["attempts"] == before["tasks"][0]["attempts"] == 1
    assert mission["blockers"] == []
    transition_mission(repo, id, "IMPLEMENTING")  # scope checks pass against the new constitution


def test_blank_reference_exception_does_not_reconcile(repo):
    id = implementing(repo)
    new_hash = amend_constitution(repo)
    transition_mission(repo, id, "BLOCKED", reason="Constitution changed")
    mission = load_mission(repo, id)
    mission["decisions"].append(
        {
            "id": "D-BLANK",
            "kind": "exception",
            "subject_hash": new_hash,
            "reference": "  ",
            "recorded_at": "x",
        }
    )
    (repo / f".factory/missions/{id}/mission.json").write_text(json.dumps(mission))
    with pytest.raises(FactoryError, match="Constitution changes require an exception decision"):
        accept_scope(repo, id)


def test_scope_decision_does_not_reconcile_constitution(repo):
    id = implementing(repo)
    new_hash = amend_constitution(repo)
    transition_mission(repo, id, "BLOCKED", reason="Constitution changed")
    record_decision(repo, id, {"id": "D-S2", "kind": "scope", "subject_hash": new_hash, "reference": "chat"})
    with pytest.raises(FactoryError, match="Constitution changes require an exception decision"):
        accept_scope(repo, id)


def test_maintenance_mission_reconciles_as_before(repo):
    id = implementing(repo, kind="maintenance")
    new_hash = amend_constitution(repo)
    with pytest.raises(FactoryError, match="Constitution changed"):
        transition_mission(repo, id, "VERIFYING")
    transition_mission(repo, id, "BLOCKED", reason="Constitution changed")
    with pytest.raises(FactoryError, match="exception decision"):
        accept_scope(repo, id)
    exception(repo, id, new_hash)
    mission = accept_scope(repo, id)
    assert mission["state"] == "PLANNED" and mission["constitution_hash"] == new_hash
    assert mission["constitution_version"] == "2.1.0" and mission["tasks"][0]["attempts"] == 1


def test_unversioned_constitution_drops_stale_version(repo):
    id = implementing(repo)
    path = repo / CONSTITUTION_PATH
    path.write_text(re.sub(r"^Version: .*\n", "", path.read_text(), count=1, flags=re.MULTILINE))
    new_hash = hash_file(repo, CONSTITUTION_PATH)
    commit(repo, "Drop the constitution version")
    transition_mission(repo, id, "BLOCKED", reason="Constitution changed")
    exception(repo, id, new_hash)
    mission = accept_scope(repo, id)
    assert mission["constitution_hash"] == new_hash and "constitution_version" not in mission


def test_old_missions_without_constitution_version_stay_valid(repo):
    id = begin(repo)
    path = repo / f".factory/missions/{id}/mission.json"
    mission = json.loads(path.read_text())
    del mission["constitution_version"]
    path.write_text(json.dumps(mission))
    assert "constitution_version" not in load_mission(repo, id)


def _older_constitution(root):
    """Make the installed constitution predate this release, as an older release's would."""
    path = root / CONSTITUTION_PATH
    data = b"# Software Factory Constitution\n\nVersion: 1.0.0\n\nEarlier rules.\n"
    path.write_bytes(data)
    manifest_path = root / ".factory/installation.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["files"][CONSTITUTION_PATH]["sha256"] = sha256(data)
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    return sha256(data)


def _mission_fixture(root, id, state, constitution_hash, version=None):
    record = {"id": id, "state": state, "constitution_hash": constitution_hash}
    if version:
        record["constitution_version"] = version
    put(root, f".factory/missions/{id}/mission.json", record)


def test_upgrade_reports_missions_needing_constitution_reconcile(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    old_hash = _older_constitution(tmp_path)
    new_hash = sha256((asset_root() / "CONSTITUTION.md").read_bytes())
    _mission_fixture(tmp_path, "M-ACTIVE", "IMPLEMENTING", old_hash, "1.0.0")
    _mission_fixture(tmp_path, "M-LEGACY", "BLOCKED", old_hash)
    _mission_fixture(tmp_path, "M-OTHER", "PLANNED", "1" * 64)
    _mission_fixture(tmp_path, "M-DONE", "DELIVERED", old_hash, "1.0.0")
    _mission_fixture(tmp_path, "M-GONE", "CANCELED", old_hash)
    _mission_fixture(tmp_path, "M-CURRENT", "PLANNED", new_hash, "2.0.0")
    expected = [
        {"id": "M-ACTIVE", "state": "IMPLEMENTING", "from_version": "1.0.0", "to_version": "2.0.0"},
        {"id": "M-LEGACY", "state": "BLOCKED", "from_version": "1.0.0", "to_version": "2.0.0"},
        {"id": "M-OTHER", "state": "PLANNED", "from_version": None, "to_version": "2.0.0"},
    ]
    for dry_run in (True, False):
        report = install(tmp_path, upgrade=True, skip_sync=True, dry_run=dry_run)
        assert report["missions_needing_constitution_reconcile"] == expected
        note = report["constitution_reconcile_note"]
        assert new_hash in note and f'"id": "D-CONST-{new_hash[:8]}"' in note
        assert "software-factory mission block --mission <id>" in note
        assert "software-factory mission accept-scope --mission <id>" in note
    assert hash_file(tmp_path, CONSTITUTION_PATH) == new_hash
    unchanged = install(tmp_path, upgrade=True, skip_sync=True, dry_run=True)
    assert "missions_needing_constitution_reconcile" not in unchanged


def test_upgrade_with_changed_constitution_and_no_missions_reports_empty_list(tmp_path):
    install(tmp_path, selected="codex", skip_sync=True)
    _older_constitution(tmp_path)
    report = install(tmp_path, upgrade=True, skip_sync=True, dry_run=True)
    assert report["missions_needing_constitution_reconcile"] == []
    assert "constitution_reconcile_note" not in report


def test_always_loaded_instructions_stay_within_budget(tmp_path):
    install(tmp_path, selected="claude,codex,copilot", skip_sync=True)
    text = (tmp_path / "AGENTS.md").read_text()
    block = text[text.index(START) : text.index(END) + len(END)]
    size = len(block.encode())
    assert size <= AGENTS_BLOCK_BUDGET, (
        f"AGENTS.md managed block is {size} bytes (budget {AGENTS_BLOCK_BUDGET})"
    )
    words = len((asset_root() / "CONSTITUTION.md").read_text().split())
    assert words <= CONSTITUTION_WORD_LIMIT, f"CONSTITUTION.md has {words} words"


def test_exported_agents_defer_to_generated_briefs(tmp_path):
    install(tmp_path, selected="claude,codex,copilot", skip_sync=True)
    constitution = sha256((tmp_path / CONSTITUTION_PATH).read_bytes())
    bodies = [p.read_text() for p in (tmp_path / ".claude/agents").glob("*.md")]
    assert bodies
    for body in bodies:
        assert "Require objective, permitted paths" not in body
    specialist = [b for b in bodies if "Assigned skill:" in b]
    assert specialist
    for body in specialist:
        assert f"(SHA-256 {constitution})" in body
        assert (
            "Work only from the generated brief the orchestrator provides (software-factory mission brief); "
            "report missing brief fields instead of guessing." in body
        )
