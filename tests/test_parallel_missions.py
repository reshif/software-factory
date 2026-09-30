"""Missions in parallel: one worktree and branch per mission, and no file overlap between them."""

from __future__ import annotations

import pytest
from test_lanes import ID, add, scoped
from test_mission_030 import REQUEST, author, cli, criteria, put, repo  # noqa: F401  (fixture)

from software_factory.core import FactoryError, git
from software_factory.workflow import load_mission, transition_mission, transition_task

OTHER = "M-TWO"


def worktree_mission(root, mission_id=OTHER):
    request = put(root, ".factory/local/other-request.md", REQUEST)
    return cli(
        root, "mission", "create", "--id", mission_id, "--title", "Parallel", "--kind", "patch",
        "--request-file", request, "--worktree", "--skip-sync",
    )  # fmt: skip


def prepare(path, mission_id, task_id, owned):
    author(path, mission_id)
    criteria(path, mission_id)
    cli(path, "mission", "accept-scope", "--mission", mission_id)
    task = {
        "id": task_id,
        "title": "Other work",
        "owned_paths": owned,
        "checks": ["unit"],
        "criteria": ["AC-1"],
    }
    cli(
        path,
        "mission",
        "task-add",
        "--mission",
        mission_id,
        "--input",
        put(path, ".factory/local/t.json", task),
    )
    transition_mission(path, mission_id, "IMPLEMENTING")


def test_worktree_mission_gets_its_own_tree_and_branch(repo):  # noqa: F811
    out = worktree_mission(repo)
    path = repo.parent / f"{repo.name}.missions" / OTHER
    assert (
        out["workspace"] == str(path) and out["branch"] == f"factory/{OTHER}" and out["state"] == "PROPOSED"
    )
    assert (path / f".factory/missions/{OTHER}/request.md").read_text() == REQUEST
    assert not (repo / f".factory/missions/{OTHER}").exists()
    assert git(path, "branch", "--show-current") == f"factory/{OTHER}"
    assert "Open your coding client in" in out["next"]
    listed = cli(repo, "mission", "list", "--all")
    assert [m["id"] for m in listed["worktrees"][0]["missions"]] == [OTHER]


def test_tasks_in_parallel_missions_may_not_touch_the_same_files(repo):  # noqa: F811
    scoped(repo)
    add(repo, "T-1", ["src/**"])
    transition_mission(repo, ID, "IMPLEMENTING")
    transition_task(repo, ID, "T-1", "RUNNING")
    worktree_mission(repo)
    path = repo.parent / f"{repo.name}.missions" / OTHER
    prepare(path, OTHER, "T-A", ["src/app.py"])
    with pytest.raises(FactoryError, match=f"may touch the same files as task T-1 of mission {ID}"):
        transition_task(path, OTHER, "T-A", "RUNNING")
    assert load_mission(path, OTHER)["tasks"][0]["status"] == "TODO"


def test_parallel_missions_on_different_files_run_together(repo):  # noqa: F811
    scoped(repo)
    add(repo, "T-1", ["src/**"])
    transition_mission(repo, ID, "IMPLEMENTING")
    transition_task(repo, ID, "T-1", "RUNNING")
    worktree_mission(repo)
    path = repo.parent / f"{repo.name}.missions" / OTHER
    prepare(path, OTHER, "T-A", ["docs/**"])
    transition_task(path, OTHER, "T-A", "RUNNING")
    assert load_mission(path, OTHER)["tasks"][0]["status"] == "RUNNING"
    assert load_mission(repo, ID)["tasks"][0]["status"] == "RUNNING"


def test_failed_worktree_mission_is_rolled_back(repo):  # noqa: F811
    request = put(repo, ".factory/local/other-request.md", REQUEST)
    with pytest.raises(FactoryError):
        cli(repo, "mission", "create", "--id", OTHER, "--title", "Bad", "--kind", "nonsense",
            "--request-file", request, "--worktree", "--skip-sync")  # fmt: skip
    assert not (repo.parent / f"{repo.name}.missions" / OTHER).exists()
    assert git(repo, "branch", "--list", f"factory/{OTHER}") == ""
