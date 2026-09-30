"""Hash-chained mission event log: ``.factory/missions/<id>/events.jsonl``.

Every write of ``mission.json`` through the CLI appends one event: the command, who ran it
(OS user and client session when known), the resulting state, version and task statuses,
the SHA-256 of the ``mission.json`` just written and the hash of the previous event. The
gate re-checks the chain and compares the last event with the current ``mission.json``, so
an edit made outside the CLI, or a removed or altered event, is visible. It is tamper-evident,
not tamper-proof: someone who rewrites both files and the whole chain can still forge
history, which Git history of the committed records would show.
"""

from __future__ import annotations

import contextvars
import getpass
import hashlib
import json
import os

from .core import now, safe_path

COMMAND: contextvars.ContextVar[str] = contextvars.ContextVar("software_factory_command", default="api")
SESSION_VARIABLES = ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "CODEX_SESSION_ID", "COPILOT_SESSION_ID")
LABEL_OPTIONS = ("--task", "--to", "--kind", "--doc", "--revision")


def events_path(mission_id: str) -> str:
    return f".factory/missions/{mission_id}/events.jsonl"


def command_label(argv: list[str]) -> str:
    """A short, secret-free label for a command line: its words plus a few identifying options."""
    words = []
    for arg in argv:
        if arg.startswith("-") or len(words) == 2:
            break
        words.append(arg)
    for index, arg in enumerate(argv[:-1]):
        if arg in LABEL_OPTIONS:
            words.append(f"{arg} {argv[index + 1]}")
    return " ".join(words) or "api"


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _actor() -> dict:
    try:
        user = getpass.getuser()
    except (KeyError, OSError):
        user = None
    session = next((os.environ[k] for k in SESSION_VARIABLES if os.environ.get(k)), None)
    return {"user": user, "session": session}


def read_events(root, mission_id: str) -> list[dict]:
    target = safe_path(root, events_path(mission_id))
    if not target.is_file():
        return []
    return [json.loads(line) for line in target.read_text(encoding="utf-8").splitlines() if line.strip()]


def append_event(root, mission_id: str, mission: dict) -> dict:
    """Append the event for the mission.json just written; call it under the state lock."""
    target = safe_path(root, events_path(mission_id))
    lines = target.read_text(encoding="utf-8").splitlines() if target.is_file() else []
    last = json.loads(lines[-1]) if lines else None
    written = safe_path(root, f".factory/missions/{mission_id}/mission.json").read_bytes()
    event = {
        "seq": (last["seq"] + 1) if last else 1,
        "at": now(),
        "command": COMMAND.get(),
        "actor": _actor(),
        "state": mission["state"],
        "version": mission.get("version", 0),
        "tasks": {t["id"]: t["status"] for t in mission.get("tasks", [])},
        "mission_sha256": hashlib.sha256(written).hexdigest(),
        "prev": last["hash"] if last else None,
    }
    event["hash"] = hashlib.sha256(_canonical(event).encode()).hexdigest()
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(_canonical(event) + "\n")
    if event["state"] == "BLOCKED" and (not last or last.get("state") != "BLOCKED"):
        from .watch import notify

        blockers = mission.get("blockers") or [{}]
        notify(root, "mission_blocked", mission_id, "BLOCKED", blockers[-1].get("reason"))
    return event


def verify_events(root, mission_id: str) -> dict:
    """Check the chain and that the last event matches the current mission.json."""
    target = safe_path(root, events_path(mission_id))
    if not target.is_file():
        return {"present": False, "count": 0, "problems": []}
    problems, previous = [], None
    try:
        events = read_events(root, mission_id)
    except ValueError as exc:
        return {"present": True, "count": 0, "problems": [f"events.jsonl is not valid JSON lines ({exc})"]}
    for index, event in enumerate(events, 1):
        body = {k: v for k, v in event.items() if k != "hash"}
        if event.get("seq") != index:
            problems.append(
                f"event {index} has sequence {event.get('seq')}; an event was removed or reordered"
            )
        if hashlib.sha256(_canonical(body).encode()).hexdigest() != event.get("hash"):
            problems.append(f"event {index} was altered (its hash does not match its content)")
        if event.get("prev") != (previous["hash"] if previous else None):
            problems.append(f"event {index} does not follow event {index - 1}; the chain is broken")
        previous = event
    current = safe_path(root, f".factory/missions/{mission_id}/mission.json").read_bytes()
    if previous and previous.get("mission_sha256") != hashlib.sha256(current).hexdigest():
        problems.append(
            "mission.json changed outside the software-factory CLI since the last recorded event "
            f"(event {previous.get('seq')}, {previous.get('command')})"
        )
    return {"present": True, "count": len(events), "problems": problems}


def mission_history(root, mission_id: str) -> dict:
    from .workflow import load_mission

    load_mission(root, mission_id)
    return {"mission": mission_id, **verify_events(root, mission_id), "events": read_events(root, mission_id)}
