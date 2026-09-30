"""Parallel lanes: tasks run side by side in their own worktrees and integrate into the mission tree."""

from __future__ import annotations

import pytest
from test_lanes import ID, add, scoped
from test_mission_030 import brief, cli, repo  # noqa: F401  (fixture)
from test_workflow import result_for

from software_factory.checks import verify_mission
from software_factory.core import FactoryError
from software_factory.workflow import load_mission, record_result, transition_mission, transition_task


def lanes_ready(root):
    scoped(root)
    add(root, "T-1", ["src/limiter.py"])
    add(root, "T-2", ["src/login.py"])
    transition_mission(root, ID, "IMPLEMENTING")


def lane(root, command, task, *extra):
    return cli(root, "mission", command, "--mission", ID, "--task", task, *extra)


def status(root, task):
    return next(t["status"] for t in load_mission(root, ID)["tasks"] if t["id"] == task)


def test_two_lanes_run_at_once_and_integrate_into_the_mission_tree(repo):  # noqa: F811
    lanes_ready(repo)
    (repo / "src/shared.py").write_text("SHARED = 1\n")  # Uncommitted candidate state.
    one, two = lane(repo, "lane-open", "T-1"), lane(repo, "lane-open", "T-2")
    assert status(repo, "T-1") == status(repo, "T-2") == "RUNNING"
    lane_one, lane_two = repo / one["path"], repo / two["path"]
    assert (lane_one / "src/shared.py").read_text() == "SHARED = 1\n"  # The lane starts from the candidate.
    (lane_one / "src/limiter.py").write_text("LIMIT = 5\n")
    (lane_two / "src/login.py").write_text("STATUS = 429\n")
    assert not (repo / "src/limiter.py").exists()  # Nothing reaches the mission tree before integration.
    text = (repo / brief_task(repo, "T-1")).read_text()
    assert "## Lane" in text and one["path"] in text

    out = lane(repo, "lane-integrate", "T-2")
    assert out["integrated"] == ["src/login.py"] and out["state"] == "VERIFYING"
    assert (repo / "src/login.py").read_text() == "STATUS = 429\n" and not lane_two.exists()
    # One active task in the mission tree: T-1 integrates after T-2 is verified and DONE.
    with pytest.raises(FactoryError, match="Task T-2 is active in the mission tree"):
        lane(repo, "lane-integrate", "T-1")
    finish_task(repo, "T-2", ["src/login.py"], "R-1")
    lane(repo, "lane-integrate", "T-1")
    assert (repo / "src/limiter.py").read_text() == "LIMIT = 5\n"
    assert cli(repo, "mission", "lanes", "--mission", ID)["open_lanes"] == []
    finish_task(repo, "T-1", ["src/limiter.py"], "R-2")
    assert status(repo, "T-1") == status(repo, "T-2") == "DONE"


def finish_task(root, task, changed, revision):
    verified = verify_mission(root, ID, revision)
    assert verified["pass"], verified
    result = result_for(root, ID, verified, changed, task)
    result["criteria_evidence"] = {"AC-1": ["check:unit"]}
    record_result(root, ID, result)
    transition_task(root, ID, task, "DONE")


def brief_task(root, task):
    return cli(root, "mission", "brief", "--mission", ID, "--task", task)["path"]


def test_integration_refuses_edits_outside_owned_paths(repo):  # noqa: F811
    lanes_ready(repo)
    opened = lane(repo, "lane-open", "T-1")
    (repo / opened["path"] / "src/limiter.py").write_text("LIMIT = 5\n")
    (repo / opened["path"] / "src/app.py").write_text("VALUE = 99\n")
    with pytest.raises(FactoryError, match="changed paths outside its owned paths: src/app.py"):
        lane(repo, "lane-integrate", "T-1")
    assert (repo / "src/app.py").read_text() == "VALUE = 1\n"


def test_integration_refuses_files_changed_in_the_mission_tree_meanwhile(repo):  # noqa: F811
    lanes_ready(repo)
    opened = lane(repo, "lane-open", "T-1")
    (repo / opened["path"] / "src/limiter.py").write_text("LIMIT = 5\n")
    (repo / "src/limiter.py").write_text("LIMIT = 7\n")  # Someone wrote it in the mission tree.
    with pytest.raises(
        FactoryError, match="changed in the mission tree since lane T-1 opened: src/limiter.py"
    ):
        lane(repo, "lane-integrate", "T-1")
    assert (repo / "src/limiter.py").read_text() == "LIMIT = 7\n"


def test_a_task_running_in_the_mission_tree_still_runs_alone(repo):  # noqa: F811
    lanes_ready(repo)
    transition_task(repo, ID, "T-1", "RUNNING")  # Not in a lane: writes the mission tree.
    with pytest.raises(FactoryError, match="Task T-1 is running in the mission working tree"):
        lane(repo, "lane-open", "T-2")
    assert load_mission(repo, ID)["tasks"][1]["status"] == "TODO"
    assert not (repo / ".factory/local/lanes" / ID / "T-2").exists()  # Rolled back.


def test_lane_needs_its_dependencies_done(repo):  # noqa: F811
    scoped(repo)
    add(repo, "T-1", ["src/limiter.py"])
    add(repo, "T-2", ["src/routes.py"], ["T-1"])
    transition_mission(repo, ID, "IMPLEMENTING")
    with pytest.raises(FactoryError, match="Task dependencies are incomplete"):
        lane(repo, "lane-open", "T-2")


def test_close_abandons_the_lane_and_blocks_the_task(repo):  # noqa: F811
    lanes_ready(repo)
    opened = lane(repo, "lane-open", "T-1")
    out = lane(repo, "lane-close", "T-1", "--reason", "Wrong approach")
    assert out["state"] == "BLOCKED" and not (repo / opened["path"]).exists()
    assert load_mission(repo, ID)["tasks"][0]["blocked_reason"] == "Wrong approach"


def test_lane_check_runs_checks_in_the_lane_without_recording(repo):  # noqa: F811
    lanes_ready(repo)
    lane(repo, "lane-open", "T-1")
    before = len(load_mission(repo, ID)["evidence"])
    out = lane(repo, "lane-check", "T-1")
    assert out["recorded"] is False and out["lane"].endswith("/T-1")
    assert len(load_mission(repo, ID)["evidence"]) == before
