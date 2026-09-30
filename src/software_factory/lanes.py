"""Parallel lanes: one Git worktree per task, integrated back into the mission's working tree.

The mission's working tree stays the one candidate that verification, reviews and the gate
judge. A lane is a private worktree under ``.factory/local/lanes/<mission>/<task>``: it starts
as a snapshot of the current candidate, the task's implementer edits only the task's owned
paths there, and ``integrate`` copies those paths back after refusing edits outside them and
files that changed in the mission tree meanwhile. Several lanes can be open at once; a task
running directly in the mission tree still runs alone. Lanes are local state, like run logs:
mission records never depend on them, and the orchestrator alone runs these commands.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from .core import FactoryError, assert_id, now, read_json, safe_path, write_json
from .evidence import matches_path

LANE_ROOT = ".factory/local/lanes"
# A lane never carries or changes mission records or factory-local state.
PRIVATE = (".factory/missions/", ".factory/local/")
IDENTITY = (
    "-c",
    "user.name=software-factory",
    "-c",
    "user.email=software-factory@localhost",
    "-c",
    "commit.gpgsign=false",
    "-c",
    "core.hooksPath=/dev/null",
)


def lane_path(mission_id: str, task_id: str) -> str:
    return f"{LANE_ROOT}/{assert_id(mission_id)}/{assert_id(task_id)}"


def _manifest(mission_id: str, task_id: str) -> str:
    return lane_path(mission_id, task_id) + ".json"


def _git(cwd: Path, *args: str, data: bytes | None = None, ok=(0,)) -> bytes:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], input=data, capture_output=True, check=False, timeout=120, env=env
    )
    if result.returncode not in ok:
        raise FactoryError(f"git {args[0]} failed: {result.stderr.decode(errors='replace').strip()}")
    return result.stdout


def _paths(output: bytes) -> list[str]:
    return [os.fsdecode(p) for p in output.split(b"\0") if p]


def load_lane(root, mission_id: str, task_id: str) -> dict | None:
    target = _manifest(mission_id, task_id)
    return read_json(root, target) if safe_path(root, target).is_file() else None


def open_lanes(root, mission_id: str) -> list[dict]:
    directory = safe_path(root, f"{LANE_ROOT}/{assert_id(mission_id)}")
    if not directory.is_dir():
        return []
    return [read_json(root, f"{LANE_ROOT}/{mission_id}/{p.name}") for p in sorted(directory.glob("*.json"))]


def laned_tasks(root, mission_id: str) -> set[str]:
    return {lane["task"] for lane in open_lanes(root, mission_id)}


def _candidate_paths(root: Path) -> list[str]:
    """Paths where the mission working tree differs from HEAD (tracked changes and untracked files)."""
    changed = _paths(_git(root, "diff", "--name-only", "-z", "--no-renames", "HEAD"))
    untracked = _paths(_git(root, "ls-files", "--others", "--exclude-standard", "-z"))
    return sorted({p for p in changed + untracked if not p.startswith(PRIVATE)})


def _copy(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink() or target.exists():
        target.unlink()
    if source.is_symlink():
        os.symlink(os.readlink(source), target)
    else:
        shutil.copy2(source, target)


def _remove_worktree(root: Path, path: Path) -> None:
    if path.exists():
        _git(root, "worktree", "remove", "--force", str(path), ok=(0, 128))
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)
    _git(root, "worktree", "prune", ok=(0, 128))


def open_lane(root, mission_id: str, task_id: str) -> dict:
    """Create the task's worktree from the current candidate and start the task RUNNING in it."""
    from .workflow import load_mission, paths_may_overlap, transition_task

    root = Path(root).resolve()
    mission = load_mission(root, mission_id)
    task = next((t for t in mission["tasks"] if t["id"] == task_id), None)
    if task is None:
        raise FactoryError(f"Unknown task: {task_id}")
    if mission["state"] != "IMPLEMENTING":
        raise FactoryError("Lanes open only while the mission is IMPLEMENTING")
    if task["status"] not in {"TODO", "VERIFYING", "BLOCKED"}:
        raise FactoryError(
            f"Task {task_id} is {task['status']}; a lane opens for a TODO task or for a repair"
        )
    if load_lane(root, mission_id, task_id) is not None:
        raise FactoryError(f"Task {task_id} already has an open lane; integrate or close it")
    others = {t["id"]: t for t in mission["tasks"]}
    for lane in open_lanes(root, mission_id):
        shared = [
            (a, b)
            for a in task["owned_paths"]
            for b in others[lane["task"]]["owned_paths"]
            if paths_may_overlap(a, b)
        ]
        if shared:
            raise FactoryError(
                f"Task {task_id} may touch the same files as open lane {lane['task']} ({shared[0][0]} and "
                f"{shared[0][1]}); wait for that lane to integrate"
            )
    path = safe_path(root, lane_path(mission_id, task_id))
    if path.exists():
        raise FactoryError(
            f"A stale lane directory exists at {lane_path(mission_id, task_id)}; close the lane first"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    _git(root, "worktree", "add", "--detach", "--quiet", str(path), "HEAD")
    try:
        for relative in _candidate_paths(root):
            source, target = root / relative, path / relative
            if source.is_symlink() or source.exists():
                _copy(source, target)
            elif target.is_symlink() or target.exists():
                target.unlink()
        _git(path, *IDENTITY, "add", "-A")
        _git(path, *IDENTITY, "commit", "--quiet", "--no-verify", "--allow-empty", "-m", "lane snapshot")
        base = _git(path, "rev-parse", "HEAD").decode().strip()
        manifest = {
            "schema_version": 1,
            "mission": mission_id,
            "task": task_id,
            "path": lane_path(mission_id, task_id),
            "base": base,
            "owned_paths": task["owned_paths"],
            "opened_at": now(),
        }
        write_json(root, _manifest(mission_id, task_id), manifest)
        record = transition_task(root, mission_id, task_id, "RUNNING")
    except Exception:
        manifest_file = safe_path(root, _manifest(mission_id, task_id))
        if manifest_file.exists():
            manifest_file.unlink()
        _remove_worktree(root, path)
        raise
    status = next(t for t in record["tasks"] if t["id"] == task_id) if isinstance(record, dict) else {}
    return {**manifest, "attempt": status.get("attempts"), "state": status.get("status", "RUNNING")}


def _lane_changes(path: Path, base: str) -> list[tuple[str, str]]:
    _git(path, *IDENTITY, "add", "-A")
    out = _paths(_git(path, "diff", "--cached", "--name-status", "-z", "--no-renames", base))
    return [(out[i], out[i + 1]) for i in range(0, len(out), 2)]


def integrate_lane(root, mission_id: str, task_id: str) -> dict:
    """Copy the lane's owned-path changes into the mission tree, remove the lane, move the task to VERIFYING."""
    from .workflow import load_mission, transition_task

    root = Path(root).resolve()
    lane = load_lane(root, mission_id, task_id)
    if lane is None:
        raise FactoryError(f"Task {task_id} has no open lane")
    mission = load_mission(root, mission_id)
    task = next(t for t in mission["tasks"] if t["id"] == task_id)
    if task["status"] != "RUNNING":
        raise FactoryError(f"Task {task_id} is {task['status']}; only a RUNNING lane can be integrated")
    # Constitution, One writer: the mission tree holds one active task at a time, so lanes
    # integrate one after another; each finishes verify, record-result and DONE first.
    laned = laned_tasks(root, mission_id)
    busy = [
        t["id"] for t in mission["tasks"] if t["id"] not in laned and t["status"] in {"RUNNING", "VERIFYING"}
    ]
    if busy:
        raise FactoryError(
            f"Task {busy[0]} is active in the mission tree; finish it (verify, record-result, DONE) before "
            f"integrating lane {task_id}"
        )
    path = safe_path(root, lane["path"])
    changes = _lane_changes(path, lane["base"])
    outside = [
        p
        for _, p in changes
        if p.startswith(PRIVATE) or not any(matches_path(p, o) for o in task["owned_paths"])
    ]
    if outside:
        raise FactoryError(
            f"Lane {task_id} changed paths outside its owned paths: {', '.join(outside[:10])}; revert them in the "
            "lane, or replan the task's owned paths"
        )
    conflicts = []
    for _, relative in changes:
        kind = _git(path, "cat-file", "-t", f"{lane['base']}:{relative}", ok=(0, 128)).strip()
        base_bytes = _git(path, "cat-file", "blob", f"{lane['base']}:{relative}") if kind == b"blob" else None
        current = root / relative
        now_bytes = current.read_bytes() if current.is_file() and not current.is_symlink() else None
        if current.is_symlink():
            now_bytes = os.fsencode(os.readlink(current))
        if now_bytes != base_bytes:
            conflicts.append(relative)
    if conflicts:
        raise FactoryError(
            f"These files changed in the mission tree since lane {task_id} opened: {', '.join(conflicts[:10])}; "
            f"close the lane and open it again from the current candidate"
        )
    integrated, removed = [], []
    for status, relative in changes:
        target = root / relative
        if status == "D":
            if target.is_symlink() or target.exists():
                target.unlink()
            removed.append(relative)
        else:
            _copy(path / relative, target)
            integrated.append(relative)
    safe_path(root, _manifest(mission_id, task_id)).unlink()
    _remove_worktree(root, path)
    transition_task(root, mission_id, task_id, "VERIFYING")
    return {
        "task": task_id,
        "integrated": integrated,
        "removed": removed,
        "state": "VERIFYING",
        "next": f"software-factory verify --mission {mission_id} --revision <new label>, then record-result",
    }


def close_lane(root, mission_id: str, task_id: str, reason: str) -> dict:
    """Abandon a lane: remove its worktree; a RUNNING task becomes BLOCKED with the reason."""
    from .workflow import load_mission, transition_task

    root = Path(root).resolve()
    lane = load_lane(root, mission_id, task_id)
    path = safe_path(root, lane_path(mission_id, task_id))
    if lane is None and not path.exists():
        raise FactoryError(f"Task {task_id} has no open lane")
    if lane is not None:
        safe_path(root, _manifest(mission_id, task_id)).unlink()
    _remove_worktree(root, path)
    task = next((t for t in load_mission(root, mission_id)["tasks"] if t["id"] == task_id), None)
    if task and task["status"] == "RUNNING":
        transition_task(root, mission_id, task_id, "BLOCKED", reason=reason)
        return {"task": task_id, "closed": True, "state": "BLOCKED"}
    return {"task": task_id, "closed": True, "state": task["status"] if task else None}


def check_lane(root, mission_id: str, task_id: str, only=None) -> dict:
    """Run the configured checks inside the lane; advisory only, nothing is recorded."""
    from .checks import run_checks

    root = Path(root).resolve()
    lane = load_lane(root, mission_id, task_id)
    if lane is None:
        raise FactoryError(f"Task {task_id} has no open lane")
    result = run_checks(safe_path(root, lane["path"]), only)
    return {**result, "lane": lane["path"], "recorded": False}
