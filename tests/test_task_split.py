"""task-split: the orchestrator narrows a task that has not started into parallel lanes."""

from __future__ import annotations

import pytest
from test_lanes import ID, add, scoped
from test_mission_030 import cli, put, repo  # noqa: F401  (fixture)

from software_factory.core import FactoryError
from software_factory.workflow import load_mission, transition_mission, transition_task


def split(root, task, value):
    return cli(
        root,
        "mission",
        "task-split",
        "--mission",
        ID,
        "--task",
        task,
        "--input",
        put(root, ".factory/local/s.json", value),
    )


def two_way(**overrides):
    value = {
        "reason": "Limiter and login are independent",
        "into": [
            {"id": "T-1a", "title": "Limiter", "owned_paths": ["src/limiter/**"], "criteria": ["AC-1"]},
            {"id": "T-1b", "title": "Login", "owned_paths": ["src/login/**"], "criteria": ["AC-1"]},
        ],
    }
    value.update(overrides)
    return value


def test_split_replaces_the_task_and_repoints_dependents(repo):  # noqa: F811
    scoped(repo)
    add(repo, "T-0", ["src/contract.py"])
    add(repo, "T-1", ["src/**"], ["T-0"])
    add(repo, "T-2", ["docs/**"], ["T-1"])
    split(repo, "T-1", two_way())
    mission = load_mission(repo, ID)
    tasks = {t["id"]: t for t in mission["tasks"]}
    assert list(tasks) == ["T-0", "T-1a", "T-1b", "T-2"]
    assert tasks["T-1a"]["depends_on"] == ["T-0"] and tasks["T-1b"]["depends_on"] == ["T-0"]
    assert tasks["T-2"]["depends_on"] == ["T-1a", "T-1b"]
    history = mission["task_history"][-1]
    assert history["task"]["id"] == "T-1" and history["split_into"] == ["T-1a", "T-1b"]
    lanes = cli(repo, "mission", "lanes", "--mission", ID)
    assert lanes["waves"] == [["T-0"], ["T-1a", "T-1b"], ["T-2"]]


def test_split_may_only_narrow_owned_paths(repo):  # noqa: F811
    scoped(repo)
    add(repo, "T-1", ["src/**"])
    wider = two_way(into=[{"id": "T-1a", "title": "A", "owned_paths": ["src/a.py"], "criteria": ["AC-1"]},
                          {"id": "T-1b", "title": "B", "owned_paths": ["docs/b.md"], "criteria": ["AC-1"]}])  # fmt: skip
    with pytest.raises(FactoryError, match="may only narrow T-1's owned paths .* not covered: docs/b.md"):
        split(repo, "T-1", wider)


def test_split_keeps_the_criteria(repo):  # noqa: F811
    scoped(repo)
    add(repo, "T-1", ["src/**"])
    dropped = two_way(into=[{"id": "T-1a", "title": "A", "owned_paths": ["src/a.py"]},
                            {"id": "T-1b", "title": "B", "owned_paths": ["src/b.py"]}])  # fmt: skip
    with pytest.raises(FactoryError, match="must carry exactly T-1's criteria"):
        split(repo, "T-1", dropped)


def test_split_parts_that_overlap_must_be_ordered(repo):  # noqa: F811
    scoped(repo)
    add(repo, "T-1", ["src/**"])
    overlapping = two_way(into=[{"id": "T-1a", "title": "A", "owned_paths": ["src/**"], "criteria": ["AC-1"]},
                                {"id": "T-1b", "title": "B", "owned_paths": ["src/b.py"], "criteria": ["AC-1"]}])  # fmt: skip
    with pytest.raises(FactoryError, match="could run at the same time"):
        split(repo, "T-1", overlapping)


def test_a_started_task_cannot_be_split(repo):  # noqa: F811
    scoped(repo)
    add(repo, "T-1", ["src/**"])
    transition_mission(repo, ID, "IMPLEMENTING")
    transition_task(repo, ID, "T-1", "RUNNING")
    with pytest.raises(FactoryError, match="Only a TODO task that has not started can be split"):
        split(repo, "T-1", two_way())
