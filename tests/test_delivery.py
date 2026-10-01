"""0.3.8: the factory commits a mission's work and records, publishes on approval and syncs CI."""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest
from test_workflow import begin, commit, complete, finish, git, make_repo

from software_factory import delivery
from software_factory.core import FactoryError
from software_factory.evidence import candidate_snapshot, content_head, fingerprint
from software_factory.workflow import (
    approve_decision,
    assess_gate,
    load_mission,
    transition_mission,
    transition_task,
)


def identify(root):
    git(root, "config", "user.name", "Factory Test")
    git(root, "config", "user.email", "test@example.invalid")


@pytest.fixture
def repo(tmp_path):
    root = make_repo(tmp_path / "project")
    identify(root)
    return root


def test_records_only_commits_keep_the_content_head(repo):
    base = git(repo, "rev-parse", "HEAD")
    (repo / ".factory/missions/M-ONE").mkdir(parents=True)
    (repo / ".factory/missions/M-ONE/mission.json").write_text("{}\n")
    records = commit(repo, "records")
    assert records != base and content_head(repo, records) == base
    (repo / ".factory/missions/M-ONE/notes.txt").write_text("not a record file\n")
    other = commit(repo, "a file outside the record layout")
    assert content_head(repo, other) == other
    (repo / "src/app.py").write_text("VALUE = 3\n")
    product = commit(repo, "product")
    assert content_head(repo, product) == product


def test_verify_commits_the_task_work_on_a_work_branch(repo):
    id = begin(repo)
    (repo / "src/app.py").write_text("VALUE = 2\n")
    (repo / "notes.txt").write_text("not owned by the task\n")
    transition_task(repo, id, "T-ONE", "VERIFYING")
    transition_mission(repo, id, "VERIFYING")
    from software_factory.checks import verify_mission

    verified = verify_mission(repo, id, "R-ONE")
    assert verified["pass"], verified
    committed = verified["committed"]
    assert committed["branch"] == "factory/M-ONE" and committed["paths"] == ["src/app.py"]
    assert git(repo, "symbolic-ref", "--short", "HEAD") == "factory/M-ONE"
    assert load_mission(repo, id)["branch"] == "factory/M-ONE"
    head = git(repo, "rev-parse", "HEAD")
    assert verified["head"] == head == committed["commit"]
    assert git(repo, "show", "--name-only", "--format=", head).split() == ["src/app.py"]
    assert "notes.txt" in candidate_snapshot(repo)["dirty_paths"]  # not the task's, left alone
    assert git(repo, "rev-parse", "main") != head  # the trunk did not move


def test_ready_pr_commits_records_without_staling_the_review(repo):
    id = begin(repo)
    (repo / "src/app.py").write_text("VALUE = 2\n")
    verified = complete(repo, id)
    reviewed = fingerprint(repo, load_mission(repo, id))["fingerprint"]
    assert reviewed == verified["fingerprint"]
    ready = transition_mission(repo, id, "READY_PR")
    records = ready["committed_records"]
    assert records["branch"] == "factory/M-ONE" and records["files"] > 3
    assert "approve M-ONE publish" in ready["next"]
    head = git(repo, "rev-parse", "HEAD")
    assert head == records["commit"] and content_head(repo, head) == verified["head"]
    changed = git(repo, "show", "--name-only", "--format=", head).split()
    assert changed and all(p.startswith(".factory/missions/M-ONE/") for p in changed)
    gate = assess_gate(repo, id)
    assert gate["pass"], gate
    assert fingerprint(repo, load_mission(repo, id))["fingerprint"] == reviewed


def test_without_a_git_identity_verify_warns_and_does_not_commit(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    root = make_repo(tmp_path / "anonymous")
    id = begin(root)
    (root / "src/app.py").write_text("VALUE = 2\n")
    transition_task(root, id, "T-ONE", "VERIFYING")
    transition_mission(root, id, "VERIFYING")
    from software_factory.checks import verify_mission

    verified = verify_mission(root, id, "R-ONE")
    assert verified["pass"]
    assert "not committed" in verified["committed"]["warning"]
    assert "user.name" in verified["committed"]["warning"]
    assert git(root, "symbolic-ref", "--short", "HEAD") == "main"
    assert "src/app.py" in candidate_snapshot(root)["dirty_paths"]


def test_an_existing_work_branch_is_not_taken_over(repo):
    git(repo, "branch", "factory/M-ONE")
    id = begin(repo)
    (repo / "src/app.py").write_text("VALUE = 2\n")
    with pytest.raises(FactoryError, match="already exists but is not checked out"):
        delivery.ensure_work_branch(repo, id)


FAKE_GH = """\
import json, os, sys
state_file = os.environ["FAKE_GH_STATE"]
state = json.load(open(state_file))
args = sys.argv[1:]
state.setdefault("calls", []).append(args)
def done(out="", code=0):
    json.dump(state, open(state_file, "w"))
    sys.stdout.write(out)
    sys.exit(code)
if args[:2] == ["pr", "view"]:
    if not state.get("pr"):
        done(code=1)
    if "mergeCommit" in args[-1]:
        done(json.dumps({"state": state["pr"]["state"], "mergeCommit": state["pr"].get("merge")}))
    done(json.dumps({"url": state["pr"]["url"], "state": state["pr"]["state"]}))
if args[:2] == ["pr", "create"]:
    state["pr"] = {"url": "https://github.com/acme/app/pull/7", "state": "OPEN",
                   "base": args[args.index("--base") + 1], "head": args[args.index("--head") + 1]}
    done(state["pr"]["url"] + "\\n")
if args[:2] == ["run", "list"]:
    done(json.dumps(state.get("runs", [])))
done(code=2)
"""


@pytest.fixture
def published(tmp_path, monkeypatch):
    """A READY_PR mission with a bare origin and a fake gh on PATH."""
    remote = tmp_path / "origin.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    root = make_repo(tmp_path / "project")
    identify(root)
    git(root, "remote", "add", "origin", str(remote))
    git(root, "push", "-q", "-u", "origin", "main")
    git(root, "remote", "set-head", "origin", "main")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(f"#!{sys.executable}\n" + FAKE_GH)
    gh.chmod(gh.stat().st_mode | stat.S_IEXEC)
    state = tmp_path / "gh.json"
    state.write_text("{}")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_GH_STATE", str(state))
    id = begin(root)
    (root / "src/app.py").write_text("VALUE = 2\n")
    finish(root, id)
    return root, remote, state, id


def gh_state(path):
    return json.loads(path.read_text())


def set_gh(path, **values):
    state = gh_state(path)
    state.update(values)
    path.write_text(json.dumps(state))


def test_publish_pushes_and_opens_the_pull_request_then_sync_records_ci(published):
    root, remote, state, id = published
    with pytest.raises(FactoryError, match="interactive terminal"):
        delivery.publish(root, id)
    assert gh_state(state) == {}
    done = delivery.publish(root, id, via="chat", confirm=lambda *_: None)
    assert done["branch"] == "factory/M-ONE" and done["pull_request"].endswith("/pull/7")
    head = git(root, "rev-parse", "HEAD")
    assert git(remote, "rev-parse", "refs/heads/factory/M-ONE") == head
    pr = gh_state(state)["pr"]
    assert pr["base"] == "main" and pr["head"] == "factory/M-ONE"
    assert load_mission(root, id)["delivery"]["pr_ref"] == done["pull_request"]
    assert delivery.sync(root, id)["ci"] == "none"
    set_gh(state, runs=[{"status": "in_progress", "conclusion": None, "url": "u", "workflowName": "ci"}])
    assert delivery.sync(root, id)["ci"] == "pending"
    url = "https://github.com/acme/app/actions/runs/9"
    set_gh(state, runs=[{"status": "completed", "conclusion": "success", "url": url, "workflowName": "ci"}])
    synced = delivery.sync(root, id)
    assert synced["ci"] == "success" and "approve M-ONE merge" in synced["note"]
    ci = load_mission(root, id)["delivery"]["ci_ref"]
    assert ci["url"] == url and ci["head_sha"] == head and ci["branch"] == "factory/M-ONE"
    # The user approves the merge and merges on GitHub; sync records MERGED.
    approve_decision(root, id, "merge", "Approved in chat", confirm=lambda *_: None)
    clone = root.parent / "merger"
    git(root.parent, "clone", "-q", str(remote), str(clone))
    identify(clone)
    git(clone, "merge", "-q", "--no-ff", "-m", "Merge M-ONE", "origin/factory/M-ONE")
    git(clone, "push", "-q", "origin", "main")
    merge = git(clone, "rev-parse", "HEAD")
    set_gh(state, pr={**gh_state(state)["pr"], "state": "MERGED", "merge": {"oid": merge}})
    merged = delivery.sync(root, id)
    assert merged["state"] == "MERGED" and merged["merge_ref"] == merge
    assert load_mission(root, id)["state"] == "MERGED"


def test_a_failed_ci_run_returns_the_mission_to_implementing(published):
    root, _, state, id = published
    delivery.publish(root, id, via="chat", confirm=lambda *_: None)
    set_gh(
        state,
        runs=[
            {"status": "completed", "conclusion": "failure", "url": "https://x/1", "workflowName": "tests"}
        ],
    )
    synced = delivery.sync(root, id)
    assert synced["ci"] == "failed" and synced["failed"] == ["tests"]
    mission = load_mission(root, id)
    assert (
        mission["state"] == "IMPLEMENTING"
        and mission["ci_failures"][0]["reason"] == "tests concluded failure"
    )


def test_publish_refuses_before_ready_pr_and_without_a_remote(repo):
    id = begin(repo)
    with pytest.raises(FactoryError, match="only a READY_PR mission is published"):
        delivery.publish(repo, id, confirm=lambda *_: None)
    (repo / "src/app.py").write_text("VALUE = 2\n")
    finish(repo, id)
    with pytest.raises(FactoryError, match="no remote to publish to"):
        delivery.publish(repo, id, confirm=lambda *_: None)
    assert delivery.sync(repo, id)["note"].startswith("Not published yet")


def test_chat_line_publishes_and_guard_keeps_publish_human_only(published, monkeypatch):
    root, _, state, id = published
    from importlib import util

    from software_factory.core import asset_root

    spec = util.spec_from_file_location("chat_approval_publish", asset_root() / "hooks/chat_approval.py")
    hook = util.module_from_spec(spec)
    spec.loader.exec_module(hook)
    notes = hook.record(
        root,
        {
            "hook_event_name": "UserPromptSubmit",
            "prompt": f"looks good\napprove {id} publish",
            "session_id": "s",
        },
    )
    assert len(notes) == 1 and "published M-ONE" in notes[0] and "pull/7" in notes[0]
    assert gh_state(state)["pr"]["state"] == "OPEN"
    guard_spec = util.spec_from_file_location("guard_publish", asset_root() / "hooks/orchestrator_guard.py")
    guard = util.module_from_spec(guard_spec)
    guard_spec.loader.exec_module(guard)

    def bash(command):
        return {"hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_input": {"command": command}}

    cli = "uv run --locked --project .factory software-factory"
    with pytest.raises(guard.Denied, match="approve <MISSION> publish"):
        guard.decide(bash(f"{cli} mission publish --mission {id}"))
    guard.decide(bash(f"{cli} mission sync --mission {id}"))
