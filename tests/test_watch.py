"""Stuck work and the kill switch: HALT, stale missions and the notify hook."""

from __future__ import annotations

import io
import json
import sys

import pytest
from test_lanes import ID, add, scoped
from test_mission_030 import cli, plan_mission, repo  # noqa: F401  (fixture)
from test_workflow import commit

from software_factory.core import FactoryError, read_json, write_json
from software_factory.workflow import list_missions, transition_mission, transition_task


def test_halt_stops_new_work_but_not_reading(repo):  # noqa: F811
    scoped(repo)
    add(repo, "T-1", ["src/**"])
    out = cli(repo, "mission", "halt", "--reason", "Runaway spend")
    assert out["halted"] and out["reason"] == "Runaway spend"
    with pytest.raises(
        FactoryError, match=r"halted \(Runaway spend\); moving M-REQ to IMPLEMENTING is refused"
    ):
        transition_mission(repo, ID, "IMPLEMENTING")
    assert cli(repo, "mission", "list")["halted"]["reason"] == "Runaway spend"
    assert cli(repo, "mission", "status", "--mission", ID)["state"] == "PLANNED"  # Reading still works.


def test_halt_refuses_tasks_and_lanes(repo):  # noqa: F811
    scoped(repo)
    add(repo, "T-1", ["src/**"])
    transition_mission(repo, ID, "IMPLEMENTING")
    cli(repo, "mission", "halt", "--reason", "Stop")
    with pytest.raises(FactoryError, match="starting task T-1 is refused"):
        transition_task(repo, ID, "T-1", "RUNNING")
    with pytest.raises(FactoryError, match="starting task T-1 is refused"):
        cli(repo, "mission", "lane-open", "--mission", ID, "--task", "T-1")


def test_only_an_interactive_user_lifts_the_halt(repo, monkeypatch):  # noqa: F811
    cli(repo, "mission", "halt", "--reason", "Stop")
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    with pytest.raises(FactoryError, match="only the user runs it, in an interactive terminal"):
        cli(repo, "mission", "unhalt")

    class Tty(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", Tty())
    monkeypatch.setattr(sys, "stderr", Tty())
    assert cli(repo, "mission", "unhalt") == {"halted": False}
    assert "halted" not in list_missions(repo)


def test_idle_missions_are_flagged_stale(repo):  # noqa: F811
    id = plan_mission(repo)
    assert list_missions(repo)["missions"][0]["stale"] is False
    path = f".factory/missions/{id}/mission.json"
    mission = read_json(repo, path)
    mission["updated_at"] = "2026-01-01T00:00:00Z"
    write_json(repo, path, mission)
    listed = list_missions(repo)["missions"][0]
    assert listed["stale"] is True and listed["idle_hours"] > 24


def test_notify_runs_when_a_mission_becomes_blocked(repo):  # noqa: F811
    config = read_json(repo, "factory.json")
    config["notify"] = {
        "command": [
            sys.executable,
            "-c",
            (
                "import os, json; open('.factory/local/notified.json', 'w').write(json.dumps("
                "{k: os.environ[k] for k in ('SF_EVENT', 'SF_MISSION', 'SF_STATE', 'SF_REASON')}))"
            ),
        ]
    }
    write_json(repo, "factory.json", config)
    commit(repo, "configure notify")
    id = plan_mission(repo)
    cli(repo, "mission", "block", "--mission", id, "--reason", "Need the threshold")
    seen = json.loads((repo / ".factory/local/notified.json").read_text())
    assert seen == {
        "SF_EVENT": "mission_blocked",
        "SF_MISSION": id,
        "SF_STATE": "BLOCKED",
        "SF_REASON": "Need the threshold",
    }


def test_a_failing_notify_command_never_blocks_work(repo):  # noqa: F811
    config = read_json(repo, "factory.json")
    config["notify"] = {"command": ["/nonexistent/notifier"]}
    write_json(repo, "factory.json", config)
    commit(repo, "configure notify")
    id = plan_mission(repo)
    assert cli(repo, "mission", "block", "--mission", id, "--reason", "Wait")["state"] == "BLOCKED"
