"""Read-only local Mission Deck: ``software-factory serve``.

A stdlib HTTP server bound to 127.0.0.1 shows this repository's missions (and those of its
other factory worktrees) in a browser: states, tasks, lanes, the assessment, decisions, the
event log and, on request, the live readiness gate. It only reads: every request other than
GET is refused, so nothing on the page can approve, record or change anything. API calls need
the per-session token printed at start, and the Host header must name this server, so another
web page cannot read the records through the browser (including by DNS rebinding).

``/api/activity`` feeds the Live orbit: every mission's stage, tasks and pace taken from its
event log, each event with what it changed, and the files each open lane has changed with their
age. It reads records and ``git diff`` in lane worktrees; nothing in it is estimated.
"""

from __future__ import annotations

import hmac
import json
import os
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .core import FactoryError, assert_id, asset_root, load_config

DOCS = (
    "request.md", "context.md", "assessment.md", "spec.md", "options.md", "grading.md", "plan.md", "retro.md",
    "crew-context.md",
)  # fmt: skip
MAX_DOC = 60_000
EVENTS_SHOWN = 300
ACTIVITY_EVENTS = 400
LIVE_FILES = 24


def _doc(root: Path, mission_id: str, name: str) -> str | None:
    path = root / ".factory/missions" / mission_id / name
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace")[:MAX_DOC]


def _attention(path: Path, mission: dict, item: dict) -> list[dict]:
    """What this mission needs from the user right now, in plain words."""
    from .workflow import assessment_reasons

    needs = []
    state, decisions = mission["state"], {d["kind"] for d in mission["decisions"]}
    if state == "BLOCKED" and mission.get("blockers"):
        last = mission["blockers"][-1]
        needs.append(
            {"kind": "blocked", "text": last if isinstance(last, str) else last.get("reason", "Blocked")}
        )
    elif state == "PAUSED":
        needs.append({"kind": "paused", "text": "Paused; resume it when ready"})
    if state == "PROPOSED" and "scope" not in decisions:
        try:
            assessed = mission.get("request") is not None and not assessment_reasons(path, mission)
        except (FactoryError, OSError):
            assessed = False
        needs.append(
            {
                "kind": "approve",
                "text": f"Review the assessment and spec, then reply `approve {mission['id']} scope`"
                if assessed
                else "Waiting for the interview and assessment",
            }
        )
    if state == "READY_PR":
        if not (mission.get("delivery") or {}).get("ci_ref"):
            text = "Open the pull request and record its CI result"
        elif "merge" not in decisions:
            text = f"CI is recorded: reply `approve {mission['id']} merge` when you are ready to merge"
        else:
            text = "Merge approved: merge the pull request, then record MERGED"
        needs.append({"kind": "ci", "text": text})
    if item.get("stale"):
        needs.append({"kind": "stale", "text": f"No change for {item.get('idle_hours')} h"})
    try:
        from .retro import signals

        offer = signals(path, mission["id"])
        if offer["offer"]:
            needs.append(
                {
                    "kind": "retro",
                    "text": f"A retro is worth it ({offer['signals'][0]}): ask for /factory-retro {mission['id']}",
                }
            )
    except (FactoryError, OSError, ValueError, KeyError):
        pass
    return needs


def knowledge_payload(root: Path) -> dict:
    """Project knowledge for the Deck: what is recorded, the ledger and proposals awaiting the user."""
    from .crew import list_proposals
    from .crew import status as crew_status

    try:
        report = crew_status(root)
    except (FactoryError, OSError, ValueError) as exc:
        return {"error": str(exc)}
    pending = []
    for proposal in list_proposals(root):
        if proposal.get("status") != "proposed":
            continue
        items = proposal.get("items") or []
        if proposal.get("target") == "personal":
            how = f"run `software-factory crew apply --proposal {proposal['id']}` in your terminal"
        elif items:
            how = (
                f"reply `approve {proposal['id']} crew {','.join(map(str, items))}` (or a subset) after merge"
            )
        else:
            how = f"reply `approve {proposal['id']} crew`"
        pending.append({**proposal, "approve": how})
    return {
        "project": report["project"],
        "personal": {"present": report["personal"]["present"]},
        "recipes": report["recipes"],
        "ledger": report["ledger"],
        "proposals": pending,
        "gaps": report["gaps"],
    }


def missions_payload(root: Path) -> dict:
    from .workflow import list_missions, load_mission
    from .workspaces import factory_worktrees

    def summarize(path: Path) -> list[dict]:
        out = []
        for item in list_missions(path)["missions"]:
            if "error" in item:
                out.append(item)
                continue
            mission = load_mission(path, item["id"])
            tasks = mission["tasks"]
            out.append(
                {
                    **item,
                    "attention": _attention(path, mission, item),
                    "kind": mission["kind"],
                    "tasks": {s: sum(t["status"] == s for t in tasks) for s in {t["status"] for t in tasks}},
                    "task_count": len(tasks),
                    "worktree": str(path),
                }
            )
        return out

    missions = summarize(root)
    try:
        for path in factory_worktrees(root):
            missions += summarize(path)
    except FactoryError:
        pass
    from . import __version__
    from .watch import halted

    return {
        "root": str(root),
        "project": root.name,
        "version": __version__,
        "halted": halted(root),
        "knowledge": knowledge_payload(root),
        "missions": missions,
        "generated_at": time.time(),
    }


def _locate(root: Path, mission_id: str) -> Path:
    from .workspaces import factory_worktrees

    for path in [root, *factory_worktrees(root)]:
        if (path / ".factory/missions" / mission_id / "mission.json").is_file():
            return path
    raise FactoryError(f"Unknown mission: {mission_id}")


def mission_payload(root: Path, mission_id: str) -> dict:
    from .events import read_events, verify_events
    from .workflow import load_mission, mission_lanes

    assert_id(mission_id)
    where = _locate(root, mission_id)
    mission = load_mission(where, mission_id)
    try:
        repair = load_config(where)["limits"]["repair_attempts"]
    except (FactoryError, KeyError, OSError):
        repair = None
    return {
        "worktree": str(where),
        "mission": mission,
        "repair_attempts": repair,
        "lanes": mission_lanes(where, mission_id),
        "events": {
            **verify_events(where, mission_id),
            "recent": read_events(where, mission_id)[-EVENTS_SHOWN:],
        },
        "docs": {name: _doc(where, mission_id, name) for name in DOCS},
        "options": _options(where, mission),
    }


def _options(where: Path, mission: dict) -> dict | None:
    from .options import summary

    try:
        return summary(where, mission)
    except (FactoryError, OSError):
        return None


def gate_payload(root: Path, mission_id: str) -> dict:
    from .workflow import assess_gate

    assert_id(mission_id)
    where = _locate(root, mission_id)
    gate = assess_gate(where, mission_id)
    return {
        k: gate.get(k)
        for k in ("pass", "reasons", "warnings", "lane", "risk", "required_reviews", "criteria_trace")
    }


def _diff(previous: dict | None, event: dict) -> dict:
    """What one event changed: the mission state and each task status that moved."""
    before = previous or {}
    out = {}
    if before.get("state") != event.get("state"):
        out["state"] = [before.get("state"), event.get("state")]
    old = before.get("tasks") or {}
    moved = {k: [old.get(k), v] for k, v in (event.get("tasks") or {}).items() if old.get(k) != v}
    if moved:
        out["tasks"] = moved
    return out


def _seconds(iso: str | None, at: float) -> float | None:
    from datetime import UTC, datetime

    try:
        then = datetime.fromisoformat(str(iso))
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return max(0.0, at - then.timestamp())


def _live_files(where: Path, mission_id: str, task_id: str, at: float) -> list[dict]:
    """Files an open lane has changed against its base, newest first: what an agent is writing now."""
    from .lanes import _candidate_paths, lane_path

    lane = where / lane_path(mission_id, task_id)
    if not lane.is_dir():
        return []
    try:
        paths = _candidate_paths(lane)
    except (FactoryError, OSError, ValueError):
        return []
    files = []
    for name in paths:
        try:
            age = max(0.0, at - (lane / name).stat().st_mtime)
        except OSError:
            age = None  # deleted in the lane
        files.append({"path": name, "age": None if age is None else round(age, 1)})
    files.sort(key=lambda f: f["age"] if f["age"] is not None else float("inf"))
    return files[:LIVE_FILES]


def activity_payload(root: Path, since_seq: dict | None = None) -> dict:
    """Everything the live orbit animates, from the records alone.

    Each mission carries its stage, tasks, attention and how busy it has been (events in the
    last hour and day, seconds since its last event). ``events`` are the most recent events of
    every mission with what each changed; ``since`` (``{mission: seq}``) returns only newer ones.
    Open lanes list the files their agent has changed, with how long ago each was written.
    """
    from .events import read_events
    from .lanes import open_lanes
    from .workflow import list_missions, load_mission
    from .workspaces import factory_worktrees

    at = time.time()
    try:
        places = [root, *factory_worktrees(root)]
    except FactoryError:
        places = [root]
    missions, events = [], []
    for where in places:
        for item in list_missions(where)["missions"]:
            if "error" in item:
                continue
            mission = load_mission(where, item["id"])
            try:
                log = read_events(where, mission["id"])
            except (OSError, ValueError):
                log = []
            ages = [a for a in (_seconds(e.get("at"), at) for e in log) if a is not None]
            lanes = []
            for lane in open_lanes(where, mission["id"]):
                lanes.append(
                    {
                        "task": lane["task"],
                        "opened_at": lane.get("opened_at"),
                        "files": _live_files(where, mission["id"], lane["task"], at),
                    }
                )
            missions.append(
                {
                    "id": mission["id"],
                    "title": mission.get("title") or item.get("title"),
                    "kind": mission.get("kind"),
                    "state": mission["state"],
                    "previous_state": mission.get("previous_state"),
                    "worktree": str(where),
                    "stale": bool(item.get("stale")),
                    "attention": [a["kind"] for a in _attention(where, mission, item)],
                    "tasks": [
                        {
                            "id": t["id"],
                            "title": t.get("title"),
                            "status": t["status"],
                            "depends_on": t.get("depends_on") or [],
                        }
                        for t in mission["tasks"]
                    ],
                    "lanes": lanes,
                    "pace": {
                        "hour": sum(a < 3600 for a in ages),
                        "day": sum(a < 86400 for a in ages),
                        "quiet_for": round(min(ages), 1) if ages else None,
                    },
                    "seq": log[-1]["seq"] if log else 0,
                }
            )
            after = (since_seq or {}).get(mission["id"], 0)
            previous = None
            for event in log:
                if event["seq"] > after:
                    events.append(
                        {
                            "mission": mission["id"],
                            "seq": event["seq"],
                            "at": event.get("at"),
                            "command": event.get("command"),
                            "state": event.get("state"),
                            "user": (event.get("actor") or {}).get("user"),
                            "change": _diff(previous, event),
                        }
                    )
                previous = event
    events.sort(key=lambda e: (str(e["at"]), e["seq"]))
    from .watch import halted

    return {
        "root": str(root),
        "now": at,
        "halted": halted(root),
        "missions": missions,
        "events": events[-ACTIVITY_EVENTS:],
    }


def _since(query: dict) -> dict:
    """``since=M-1:4,M-2:9`` → ``{"M-1": 4, "M-2": 9}``; malformed parts are ignored."""
    out = {}
    for part in (query.get("since") or [""])[0].split(","):
        mission, _, seq = part.partition(":")
        if mission and seq.isdigit():
            out[mission] = int(seq)
    return out


def _signature(root: Path) -> str:
    """Changes whenever a mission record (here or in other worktrees), knowledge or a proposal changes."""
    from .workspaces import factory_worktrees

    newest, count = 0.0, 0
    try:
        places = [root, *factory_worktrees(root)]
    except FactoryError:
        places = [root]
    watched = [place / ".factory/missions" for place in places]
    watched += [root / ".factory/crew", root / ".factory/local/crew/proposals"]
    for directory in watched:
        for current, _dirs, files in os.walk(directory):
            for name in files:
                try:
                    newest = max(newest, os.stat(os.path.join(current, name)).st_mtime)
                    count += 1
                except OSError:
                    continue
    return f"{newest:.6f}:{count}"


class _Handler(BaseHTTPRequestHandler):
    server_version = "software-factory-deck"

    def log_message(self, *args):  # Quiet: the terminal shows only the start line.
        return

    def _send(self, status: int, body: bytes, kind: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _host_ok(self) -> bool:
        port = self.server.server_address[1]
        return self.headers.get("Host", "") in {f"127.0.0.1:{port}", f"localhost:{port}"}

    def _token_ok(self, query: dict) -> bool:
        given = self.headers.get("X-Factory-Token") or (query.get("token") or [""])[0]
        return hmac.compare_digest(given.encode(), self.server.token.encode())

    def do_GET(self):
        if not self._host_ok():
            return self._send(421, b"Misdirected request", "text/plain")
        url = urlparse(self.path)
        query = parse_qs(url.query)
        parts = [p for p in url.path.split("/") if p]
        root = self.server.root
        try:
            if parts == ["favicon.ico"]:
                return self._send(204, b"", "image/x-icon")
            if not parts:
                page = (asset_root() / "deck/index.html").read_bytes()
                return self._send(200, page, "text/html; charset=utf-8")
            if parts[0] != "api":
                return self._send(404, b"Not found", "text/plain")
            if not self._token_ok(query):
                return self._json(401, {"error": "Missing or wrong session token"})
            if parts[1:] == ["missions"]:
                return self._json(200, missions_payload(root))
            if len(parts) == 3 and parts[1] == "mission":
                return self._json(200, mission_payload(root, parts[2]))
            if len(parts) == 4 and parts[1] == "mission" and parts[3] == "gate":
                return self._json(200, gate_payload(root, parts[2]))
            if parts[1:] == ["activity"]:
                return self._json(200, activity_payload(root, _since(query)))
            if parts[1:] == ["stream"]:
                return self._stream(root)
            return self._json(404, {"error": "Unknown endpoint"})
        except FactoryError as exc:
            return self._json(400, {"error": str(exc)})

    def _stream(self, root: Path) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        last = None
        try:
            while not self.server.stopping.is_set():
                current = _signature(root)
                if current != last:
                    self.wfile.write(f"event: changed\ndata: {current}\n\n".encode())
                    last = current
                else:
                    self.wfile.write(b": keep-alive\n\n")
                self.wfile.flush()
                time.sleep(1.0)
        except (BrokenPipeError, ConnectionResetError):
            return

    def _refuse(self):
        self._send(405, b"The Mission Deck is read-only", "text/plain")

    do_POST = do_PUT = do_PATCH = do_DELETE = _refuse


class DeckServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, root: Path, port: int):
        super().__init__(("127.0.0.1", port), _Handler)
        self.root = Path(root).resolve()
        self.token = secrets.token_urlsafe(24)
        self.stopping = threading.Event()

    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/?token={self.token}"


def serve(root: Path, port: int = 8765, announce=None) -> None:
    server = DeckServer(root, port)
    line = json.dumps({"url": server.url(), "read_only": True, "stop": "Ctrl-C"})
    (announce or (lambda text: print(text, flush=True)))(line)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.stopping.set()
        server.server_close()
