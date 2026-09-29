"""Lane-ready planning: tasks that could run together own disjoint paths, and `mission lanes`."""

from __future__ import annotations

import pytest
from test_mission_030 import author, cli, create, criteria, put, repo  # noqa: F401  (fixture)

from software_factory.core import FactoryError
from software_factory.workflow import load_mission, paths_may_overlap, transition_mission, transition_task

ID = "M-REQ"


@pytest.mark.parametrize(
    ("a", "b", "overlap"),
    [
        ("src/**", "tests/**", False),
        ("src/auth/**", "src/**", True),
        ("src/a.py", "src/b.py", False),
        ("src/a.py", "src/a.py", True),
        ("src/a.py", "src/**", True),
        ("tests/", "tests/test_x.py", True),
        ("tests/test_a.py", "tests/test_b.py", False),
        ("**/*.py", "docs/**", True),  # A leading wildcard could match anywhere: conservative.
        ("src/limiter/**", "src/login/**", False),
    ],
)
def test_paths_may_overlap(a, b, overlap):
    assert paths_may_overlap(a, b) is overlap
    assert paths_may_overlap(b, a) is overlap


def scoped(root):
    create(root, ID)
    author(root, ID)
    criteria(root, ID)
    cli(root, "mission", "accept-scope", "--mission", ID)


def add(root, task_id, owned, depends_on=()):
    task = {
        "id": task_id,
        "title": f"Task {task_id}",
        "owned_paths": list(owned),
        "depends_on": list(depends_on),
        "checks": ["unit"],
        "criteria": ["AC-1"],
    }
    return cli(
        root,
        "mission",
        "task-add",
        "--mission",
        ID,
        "--input",
        put(root, f".factory/local/{task_id}.json", task),
    )


def test_unordered_tasks_on_the_same_files_are_refused(repo):  # noqa: F811
    scoped(repo)
    add(repo, "T-1", ["src/**"])
    with pytest.raises(
        FactoryError, match=r"Tasks T-2 and T-1 could run at the same time .*src/app.py and src/\*\*"
    ):
        add(repo, "T-2", ["src/app.py"])


def test_ordered_or_disjoint_tasks_are_accepted(repo):  # noqa: F811
    scoped(repo)
    add(repo, "T-0", ["src/contract.py"])
    add(repo, "T-1", ["src/limiter/**", "tests/test_limiter.py"], ["T-0"])
    add(repo, "T-2", ["src/login/**", "tests/test_login.py"], ["T-0"])
    add(repo, "T-3", ["src/**"], ["T-1", "T-2"])  # Wiring after both: overlap is ordered.
    assert [t["id"] for t in load_mission(repo, ID)["tasks"]] == ["T-0", "T-1", "T-2", "T-3"]


def test_update_that_creates_a_conflict_is_refused(repo):  # noqa: F811
    scoped(repo)
    add(repo, "T-1", ["src/limiter/**"])
    add(repo, "T-2", ["src/login/**"])
    patch = {"owned_paths": ["src/**"], "reason": "Widen scope"}
    with pytest.raises(FactoryError, match="Tasks T-2 and T-1 could run at the same time"):
        cli(
            repo,
            "mission",
            "task-update",
            "--mission",
            ID,
            "--task",
            "T-2",
            "--input",
            put(repo, ".factory/local/u.json", patch),
        )


def test_a_finished_task_does_not_conflict(repo):  # noqa: F811
    scoped(repo)
    add(repo, "T-1", ["src/**"])
    transition_mission(repo, ID, "IMPLEMENTING")
    transition_task(repo, ID, "T-1", "RUNNING")
    (repo / "src/app.py").write_text("VALUE = 2\n")
    transition_task(repo, ID, "T-1", "VERIFYING")
    mission = load_mission(repo, ID)
    mission["tasks"][0]["status"] = "DONE"  # Only the conflict rule is under test here.
    from software_factory.core import write_json

    write_json(repo, f".factory/missions/{ID}/mission.json", mission)
    add(repo, "T-2", ["src/app.py"])


def test_mission_lanes_reports_waves_critical_path_and_readiness(repo):  # noqa: F811
    scoped(repo)
    add(repo, "T-0", ["src/contract.py"])
    add(repo, "T-1", ["src/limiter/**"], ["T-0"])
    add(repo, "T-2", ["src/login/**"], ["T-0"])
    add(repo, "T-3", ["docs/**"], ["T-0"])
    add(repo, "T-4", ["src/routes.py"], ["T-1", "T-2"])
    lanes = cli(repo, "mission", "lanes", "--mission", ID)
    assert lanes["waves"] == [["T-0"], ["T-1", "T-2", "T-3"], ["T-4"]]
    assert lanes["critical_path"] == 3 and lanes["max_parallel"] == 3 and lanes["tasks"] == 5
    assert lanes["ready"] == ["T-0"]
    assert lanes["waiting"]["T-4"] == ["T-1", "T-2"]
    assert lanes["conflicts"] == []
    assert lanes["plan_lanes_section"] is False
    assert "today tasks still run one at a time" in lanes["note"]
