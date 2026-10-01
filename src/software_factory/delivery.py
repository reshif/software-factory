"""Commits, publishing and CI made by the factory, not typed by the user (0.3.8).

Nobody in a mission could commit: the orchestrator's shell is read-only, implementers' lane work is
copied back uncommitted, yet CI must test a commit and every record is bound to the candidate's
head. So the factory commits itself, at the two points where a commit cannot make evidence stale:

- ``verify`` first commits the owned-path changes of the tasks it is about to verify, onto a work
  branch ``factory/<ID>`` when the work started on the trunk. The evidence, the task results and
  the reviews then all bind to that commit.
- READY_PR (and publishing) commits the mission's own record files. Such a commit changes only
  files of the mission record layout, which ``evidence.content_head`` skips, so the reviewed
  fingerprint stays valid.

Publishing leaves the machine, so it needs the user's own approval: ``approve <ID> publish`` in
Claude Code chat (the chat hook) or ``mission publish`` in their terminal. The factory then pushes
the work branch and opens the pull request with the user's own ``gh``. ``mission sync`` reads the
CI runs for the pushed commit from ``gh`` and records the result, and once the pull request is
merged and the user approved the merge, records the merge commit and MERGED. Records stay local and
unattested; ``gh`` output is what GitHub reports to the user's account.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .core import FactoryError, git
from .evidence import MISSION_RECORD, candidate_snapshot, matches_path

WORK_BRANCH = "factory/{id}"
NETWORK_TIMEOUT = 120
PASSING = {"success", "skipped", "neutral"}


def _branch(root) -> str | None:
    try:
        return git(root, "symbolic-ref", "--short", "HEAD")
    except FactoryError:
        return None


def _trunk_names(root) -> set[str]:
    from .workflow import select_trunk

    try:
        name = select_trunk(root)["name"]
    except FactoryError:
        return {"main", "master"}
    return {re.sub(r"^refs/heads/|^refs/remotes/[^/]+/", "", name)}


def _on_trunk(root) -> bool:
    branch = _branch(root)
    return branch is None or branch in _trunk_names(root)


def ensure_work_branch(root, mission_id: str) -> str | None:
    """Leave the trunk for ``factory/<ID>`` (keeping the working tree); the new branch, or None."""
    if not _on_trunk(root):
        return None
    name = WORK_BRANCH.format(id=mission_id)
    try:
        git(root, "show-ref", "--verify", "--quiet", f"refs/heads/{name}")
        exists = True
    except FactoryError:
        exists = False
    if exists:
        raise FactoryError(
            f"Branch {name} already exists but is not checked out; check it out (git switch {name}) and run "
            "the command again"
        )
    git(root, "switch", "-q", "-c", name)
    return name


def _identity(root) -> None:
    from .onboarding import git_identity_missing

    missing = git_identity_missing(root)
    if missing:
        raise FactoryError(
            "The factory commits your mission's work, which needs a Git identity (missing "
            + " and ".join(missing)
            + '); run git config --global user.name "Your Name" && git config --global user.email '
            "you@example.com once, then run the command again"
        )


def _commit(root, paths: list[str], message: str) -> str | None:
    """Commit exactly ``paths``; on failure unstage them again and explain."""
    from .setup_proposals import commit_paths

    try:
        return commit_paths(root, paths, message)
    except FactoryError as exc:
        try:
            git(root, "reset", "-q", "--", *paths)
        except FactoryError:
            pass
        raise FactoryError(
            f"The factory could not commit {len(paths)} file(s) ({exc}). A pre-commit hook or signing setup may "
            "have refused it; fix that and run the command again"
        ) from exc


def _record_branch(root, mission_id: str, branch: str) -> None:
    from .workflow import update_mission

    def mutate(mission):
        mission["branch"] = branch

    update_mission(root, mission_id, mutate)


def _leave_trunk(root, mission_id: str) -> None:
    """Switch to the work branch when on the trunk and record it on the mission."""
    created = ensure_work_branch(root, mission_id)
    if created:
        _record_branch(root, mission_id, created)


def _trunk_ref(root) -> str | None:
    """The trunk for ci-result: the selected one, else <remote>/<main|master> when that exists."""
    from .workflow import select_trunk

    try:
        select_trunk(root)
        return None
    except FactoryError:
        pass
    remotes = git(root, "remote").split()
    for remote in (["origin"] if "origin" in remotes else []) + remotes:
        for name in ("main", "master"):
            try:
                git(root, "show-ref", "--verify", "--quiet", f"refs/remotes/{remote}/{name}")
                return f"{remote}/{name}"
            except FactoryError:
                continue
    return None


def commit_task_work(root, mission_id: str) -> dict | None:
    """Commit the owned-path changes of the mission's VERIFYING tasks before they are verified."""
    from .workflow import load_mission

    mission = load_mission(root, mission_id)
    if mission["state"] not in {"IMPLEMENTING", "VERIFYING", "REVIEWING"}:
        return None
    tasks = [t for t in mission["tasks"] if t["status"] == "VERIFYING"]
    if not tasks:
        return None
    dirty = candidate_snapshot(root)["dirty_paths"]
    paths = [
        p
        for p in dirty
        if not MISSION_RECORD.fullmatch(p)
        and any(matches_path(p, o) for t in tasks for o in t["owned_paths"])
    ]
    if not paths:
        return None
    _identity(root)
    _leave_trunk(root, mission_id)
    names = ", ".join(f"{t['id']} {t['title']}" for t in tasks)
    sha = _commit(root, paths, f"{mission_id}: {names}\n\nCommitted by software-factory before verification.")
    return {"commit": sha, "branch": _branch(root), "paths": paths}


def _record_paths(root, mission_id: str) -> list[str]:
    directory = f".factory/missions/{mission_id}"
    listed = git(
        root, "ls-files", "-z", "--others", "--modified", "--deleted", "--exclude-standard", "--", directory
    )
    return sorted({p for p in listed.split("\0") if p and MISSION_RECORD.fullmatch(p)})


def commit_records(root, mission_id: str, reason: str) -> dict | None:
    """Commit the mission's own record files; the candidate's content head does not move."""
    if not _record_paths(root, mission_id):
        return None
    _identity(root)
    _leave_trunk(root, mission_id)
    paths = _record_paths(root, mission_id)
    sha = _commit(root, paths, f"{mission_id}: mission records ({reason})\n\nCommitted by software-factory.")
    return {"commit": sha, "branch": _branch(root), "files": len(paths)}


def _run(root, args: list[str], what: str) -> str:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GH_PROMPT_DISABLED": "1"}
    try:
        result = subprocess.run(
            args, cwd=root, env=env, capture_output=True, text=True, timeout=NETWORK_TIMEOUT, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FactoryError(f"{what} failed: {exc}") from exc
    if result.returncode:
        tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
        raise FactoryError(f"{what} failed: " + " ".join(tail))
    return result.stdout


def _remote(root) -> str:
    remotes = git(root, "remote").split()
    if not remotes:
        raise FactoryError(
            "This repository has no remote to publish to; add one (git remote add origin URL) and approve again"
        )
    return "origin" if "origin" in remotes else remotes[0]


def _gh() -> str:
    gh = shutil.which("gh")
    if not gh:
        raise FactoryError(
            "The GitHub CLI (gh) is not installed; the branch was pushed but no pull request was opened. "
            "Install gh and run gh auth login, then approve publishing again"
        )
    return gh


def _pr_body(root, mission) -> str:
    id = mission["id"]
    lines = [f"Mission {id}: {mission['title']}", ""]
    criteria = (mission.get("criteria") or {}).get("items", [])
    if criteria:
        lines += [
            "Acceptance criteria:",
            *[f"- {c['id']}: {' '.join(c['text'].split())}" for c in criteria],
            "",
        ]
    reviews = sorted({r.get("kind", "code") for r in mission.get("reviews", [])})
    lines += [
        "Local readiness gate: pass (software-factory, local-unattested)."
        + (f" Reviews: {', '.join(reviews)}." if reviews else ""),
        f"Records: .factory/missions/{id}/ (spec.md, plan.md, evidence, reviews).",
    ]
    return "\n".join(lines) + "\n"


def _confirm_on_terminal(mission_id: str, lines: list[str]) -> None:
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        raise FactoryError(
            "mission publish pushes your work, so it only runs in an interactive terminal; in Claude Code reply "
            f"`approve {mission_id} publish` instead"
        )
    sys.stderr.write("\n".join(lines) + f"\nType the mission ID ({mission_id}) to publish: ")
    sys.stderr.flush()
    if sys.stdin.readline().strip() != mission_id:
        raise FactoryError("Not published: the typed mission ID did not match")


def publish(root, mission_id: str, *, via: str = "terminal", confirm=None) -> dict:
    """Push the reviewed work branch and open (or reuse) its pull request; the user's own approval."""
    from .workflow import assess_gate, load_mission, record_delivery

    root = Path(root).resolve()
    mission = load_mission(root, mission_id)
    if mission["state"] != "READY_PR":
        raise FactoryError(f"{mission_id} is {mission['state']}; only a READY_PR mission is published")
    gate = assess_gate(root, mission_id)
    if not gate["pass"]:
        raise FactoryError("The readiness gate does not pass: " + "; ".join(gate["reasons"]))
    dirty = candidate_snapshot(root)["dirty_paths"]
    if dirty:
        raise FactoryError(
            "These product files are not committed, so they are not what was reviewed: "
            + ", ".join(dirty[:10])
            + "; run verify again (it commits the tasks' work) or remove them"
        )
    remote = _remote(root)
    branch = WORK_BRANCH.format(id=mission_id) if _on_trunk(root) else _branch(root)
    trunk = min(_trunk_names(root))
    (confirm or (lambda lines: _confirm_on_terminal(mission_id, lines)))(
        [
            f"Publish {mission_id} ({mission['title']}): push {branch} to {remote} and open a pull request into {trunk}."
        ]
    )
    _leave_trunk(root, mission_id)
    records = commit_records(root, mission_id, f"published via {via}")
    _run(root, ["git", "push", "-q", "-u", remote, f"HEAD:refs/heads/{branch}"], f"git push to {remote}")
    head = git(root, "rev-parse", "HEAD")
    gh = _gh()
    existing = None
    try:
        existing = json.loads(_run(root, [gh, "pr", "view", branch, "--json", "url,state"], "gh pr view"))
    except (FactoryError, ValueError):
        existing = None
    if existing and existing.get("state") == "OPEN":
        url = existing["url"]
    else:
        with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
            handle.write(_pr_body(root, mission))
            body = handle.name
        try:
            url = (
                _run(
                    root,
                    [
                        gh,
                        "pr",
                        "create",
                        "--head",
                        branch,
                        "--base",
                        trunk,
                        "--title",
                        f"{mission_id}: {mission['title']}",
                    ]
                    + ["--body-file", body],
                    "gh pr create",
                )
                .strip()
                .splitlines()[-1]
            )
        finally:
            Path(body).unlink(missing_ok=True)
    record_delivery(root, mission_id, {"pr_ref": url})
    return {
        "mission": mission_id,
        "branch": branch,
        "remote": remote,
        "head": head,
        "pull_request": url,
        **({"records_commit": records["commit"]} if records else {}),
        "next": f"software-factory mission sync --mission {mission_id} records CI once it finishes",
    }


def _runs(root, gh: str, head: str) -> list[dict]:
    out = _run(
        root,
        [
            gh,
            "run",
            "list",
            "--commit",
            head,
            "--limit",
            "50",
            "--json",
            "databaseId,status,conclusion,url,workflowName",
        ],
        "gh run list",
    )
    try:
        runs = json.loads(out or "[]")
    except ValueError as exc:
        raise FactoryError("gh run list returned unreadable output") from exc
    return [r for r in runs if isinstance(r, dict)]


def sync(root, mission_id: str) -> dict:
    """Record what GitHub reports: CI for the pushed commit, then the merge once approved."""
    from .workflow import load_mission, record_ci, record_delivery, transition_mission

    root = Path(root).resolve()
    mission = load_mission(root, mission_id)
    delivery = mission.get("delivery") or {}
    if mission["state"] != "READY_PR":
        return {"mission": mission_id, "state": mission["state"], "note": "Nothing to sync outside READY_PR"}
    if not delivery.get("pr_ref"):
        return {
            "mission": mission_id,
            "state": "READY_PR",
            "note": f"Not published yet: ask the user to reply `approve {mission_id} publish`",
        }
    gh = _gh()
    head = git(root, "rev-parse", "HEAD")
    if not delivery.get("ci_ref"):
        runs = _runs(root, gh, head)
        if not runs:
            return {
                "mission": mission_id,
                "ci": "none",
                "note": f"No CI runs for {head[:12]} yet; sync again later",
            }
        if any(r.get("status") != "completed" for r in runs):
            return {"mission": mission_id, "ci": "pending", "runs": len(runs)}
        failed = [r for r in runs if r.get("conclusion") not in PASSING]
        if failed:
            first = failed[0]
            record_ci(
                root, mission_id, url=first.get("url"), head=head, conclusion=first.get("conclusion") or "failure",
                reason=f"{first.get('workflowName', 'CI')} concluded {first.get('conclusion')}", trunk=_trunk_ref(root),
            )  # fmt: skip
            return {
                "mission": mission_id,
                "ci": "failed",
                "state": "IMPLEMENTING",
                "failed": [r.get("workflowName") for r in failed],
                "url": first.get("url"),
            }
        url = next(r["url"] for r in runs if r.get("conclusion") == "success")
        record_ci(root, mission_id, url=url, head=head, conclusion="success", trunk=_trunk_ref(root))
        mission = load_mission(root, mission_id)
        delivery = mission.get("delivery") or {}
    pr = json.loads(
        _run(root, [gh, "pr", "view", delivery["pr_ref"], "--json", "state,mergeCommit"], "gh pr view")
    )
    if pr.get("state") != "MERGED":
        return {
            "mission": mission_id,
            "ci": "success",
            "pull_request": pr.get("state", "unknown").lower(),
            "note": (
                f"CI passed. Ask the user to reply `approve {mission_id} merge`, then merge the pull request"
                if not any(d["kind"] == "merge" for d in mission["decisions"])
                else "CI passed and the merge is approved; sync again after the pull request is merged"
            ),
        }
    merge = (pr.get("mergeCommit") or {}).get("oid")
    if not merge:
        raise FactoryError("GitHub reports the pull request merged but gives no merge commit")
    _run(root, ["git", "fetch", "-q", _remote(root)], "git fetch")
    if not any(d["kind"] == "merge" for d in mission["decisions"]):
        return {
            "mission": mission_id,
            "pull_request": "merged",
            "note": f"Merged on GitHub; MERGED is recorded once the user replies `approve {mission_id} merge`",
        }
    record_delivery(root, mission_id, {"merge_ref": merge})
    result = transition_mission(root, mission_id, "MERGED")
    return {"mission": mission_id, "state": result["state"], "merge_ref": merge}


def handle(root, command: str, mission_id: str) -> dict:
    if command == "publish":
        return publish(root, mission_id)
    if command == "sync":
        return sync(root, mission_id)
    raise FactoryError(f"Unknown delivery command {command}")
