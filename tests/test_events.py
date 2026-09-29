"""Hash-chained mission event log: every CLI write is recorded, tampering is visible to the gate."""

from __future__ import annotations

import json
from itertools import pairwise

from test_mission_030 import assess_gate, plan_mission, repo  # noqa: F401  (fixture)

from software_factory.core import read_json, write_json
from software_factory.events import COMMAND, command_label, read_events, verify_events
from software_factory.workflow import transition_mission

ID = "M-REQ"


def events_file(root):
    return root / f".factory/missions/{ID}/events.jsonl"


def test_every_mission_write_appends_a_chained_event(repo):  # noqa: F811
    token = COMMAND.set("mission transition --to IMPLEMENTING")
    try:
        plan_mission(repo)
        transition_mission(repo, ID, "IMPLEMENTING")
    finally:
        COMMAND.reset(token)
    events = read_events(repo, ID)
    assert [e["seq"] for e in events] == list(range(1, len(events) + 1)) and len(events) >= 4
    assert events[0]["prev"] is None and all(b["prev"] == a["hash"] for a, b in pairwise(events))
    assert (
        events[-1]["state"] == "IMPLEMENTING"
        and events[-1]["command"] == "mission transition --to IMPLEMENTING"
    )
    assert set(events[-1]["actor"]) == {"user", "session"}
    assert verify_events(repo, ID) == {"present": True, "count": len(events), "problems": []}


def test_hand_edited_mission_record_is_reported_by_the_gate(repo):  # noqa: F811
    plan_mission(repo)
    path = f".factory/missions/{ID}/mission.json"
    mission = read_json(repo, path)
    mission["tasks"][0]["attempts"] = 0
    mission["title"] = "Quietly renamed"
    write_json(repo, path, mission)
    reasons = assess_gate(repo, ID)["reasons"]
    assert any("mission.json changed outside the software-factory CLI" in r for r in reasons)


def test_altered_or_removed_events_break_the_chain(repo):  # noqa: F811
    plan_mission(repo)
    lines = events_file(repo).read_text().splitlines()
    altered = json.loads(lines[1])
    altered["state"] = "READY_PR"
    events_file(repo).write_text("\n".join([lines[0], json.dumps(altered), *lines[2:]]) + "\n")
    assert any("event 2 was altered" in p for p in verify_events(repo, ID)["problems"])
    events_file(repo).write_text("\n".join([lines[0], *lines[2:]]) + "\n")
    problems = verify_events(repo, ID)["problems"]
    assert any("sequence" in p for p in problems) and any("chain is broken" in p for p in problems)


def test_missing_log_is_a_warning_not_a_failure(repo):  # noqa: F811
    plan_mission(repo)
    events_file(repo).unlink()
    gate = assess_gate(repo, ID)
    assert not any("event log" in r for r in gate["reasons"])
    assert any("No mission event log" in w for w in gate["warnings"])


def test_command_labels_keep_identifiers_not_values():
    assert command_label(
        ["mission", "task-transition", "--mission", "M-1", "--task", "T-1", "--to", "DONE"]
    ) == ("mission task-transition --task T-1 --to DONE")
    assert command_label(["verify", "--mission", "M-1", "--revision", "R-2"]) == "verify --revision R-2"
    assert command_label(["mission", "decision", "--input", "secret.json"]) == "mission decision"
