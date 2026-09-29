"""Missions in parallel: one Git worktree, branch and orchestrator session per mission.

Every mission judges its own working tree against its base commit, so two missions can
never share a tree. A parallel mission gets its own worktree next to the repository
(``<repo>.missions/<id>``) on a ``factory/<id>`` branch; the user opens a coding client
there, and that session is the mission's one orchestrator. Missions in different worktrees
see each other only here: a task may not start while an active task in another worktree may
touch the same files, so parallel missions do not collide when their branches merge.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .core import FactoryError, assert_id, safe_path, write_bytes

LANES = "/.factory/local/lanes/"


def _git(root: Path, *args: str, ok=(0,)) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=False, timeout=120, env=env
    )
    if result.returncode not in ok:
        raise FactoryError(f"git {args[0]} failed: {result.stderr.strip()}")
    return result.stdout


def mission_worktree(root: Path, mission_id: str) -> Path:
    root = Path(root).resolve()
    return root.parent / f"{root.name}.missions" / assert_id(mission_id)


def factory_worktrees(root: Path) -> list[Path]:
    """Other worktrees of this repository that hold a factory installation (lanes excluded)."""
    root = Path(root).resolve()
    found = []
    for line in _git(root, "worktree", "list", "--porcelain").splitlines():
        if not line.startswith("worktree "):
            continue
        path = Path(line[len("worktree ") :]).resolve()
        if path == root or LANES in path.as_posix() + "/":
            continue
        if (path / ".factory/installation.json").is_file() or (path / "factory.json").is_file():
            found.append(path)
    return found


def active_elsewhere(root: Path) -> list[dict]:
    """RUNNING or VERIFYING tasks of missions in the other factory worktrees."""
    from .workflow import ACTIVE_TASK_STATES, load_mission

    active = []
    for path in factory_worktrees(root):
        directory = path / ".factory/missions"
        for entry in sorted(directory.iterdir()) if directory.is_dir() else []:
            if not (entry / "mission.json").is_file():
                continue
            try:
                mission = load_mission(path, entry.name)
            except (FactoryError, OSError):
                continue
            for task in mission["tasks"]:
                if task["status"] in ACTIVE_TASK_STATES:
                    active.append({"worktree": str(path), "mission": mission["id"], "task": task})
    return active


def assert_no_cross_mission_overlap(root: Path, task: dict) -> None:
    from .workflow import paths_may_overlap

    for other in active_elsewhere(root):
        shared = [
            (a, b)
            for a in task["owned_paths"]
            for b in other["task"]["owned_paths"]
            if paths_may_overlap(a, b)
        ]
        if shared:
            raise FactoryError(
                f"Task {task['id']} may touch the same files as task {other['task']['id']} of mission "
                f"{other['mission']}, active in {other['worktree']} ({shared[0][0]} and {shared[0][1]}); "
                "wait for it to finish, or plan the work on different files"
            )


def create_mission_worktree(root: Path, value: dict, request: bytes, *, skip_sync=False) -> dict:
    """Create a worktree and branch for a new mission, set up its runtime and create the mission there."""
    from .installation import _sync
    from .workflow import create_mission

    root = Path(root).resolve()
    mission_id = assert_id(value.get("id"))
    path = mission_worktree(root, mission_id)
    branch = f"factory/{mission_id}"
    if path.exists():
        raise FactoryError(f"A worktree already exists at {path}")
    if _git(root, "branch", "--list", branch).strip():
        raise FactoryError(f"Branch {branch} already exists; pick another mission id or delete the branch")
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "--quiet", "-b", branch, str(path), "HEAD")
    try:
        if not skip_sync and (path / ".factory/pyproject.toml").is_file():
            _sync(path / ".factory")
        relative = f".factory/local/requests/{mission_id}.md"
        safe_path(path, relative).parent.mkdir(parents=True, exist_ok=True)
        write_bytes(path, relative, request)
        mission = create_mission(path, {**value, "request_file": relative}, require_request=True)
    except Exception:
        _git(root, "worktree", "remove", "--force", str(path), ok=(0, 128))
        _git(root, "branch", "-D", branch, ok=(0, 1))
        raise
    return {
        "id": mission["id"],
        "state": mission["state"],
        "workspace": str(path),
        "branch": branch,
        "next": (
            f"Open your coding client in {path} and run /factory-build (Claude Code, Copilot) or "
            f"$factory-build (Codex) to continue {mission_id}; that session is its orchestrator"
        ),
    }


def list_all(root: Path) -> list[dict]:
    from .workflow import list_missions

    return [{"worktree": str(path), **list_missions(path)} for path in factory_worktrees(root)]
