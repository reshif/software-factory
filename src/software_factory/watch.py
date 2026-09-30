"""Stuck work and the kill switch: stale missions, HALT and the notify hook.

- ``mission halt --reason TEXT`` writes ``.factory/local/HALT``. While it exists no task
  starts RUNNING, no lane opens and no mission enters IMPLEMENTING; reading, recording
  evidence and the gate keep working. Only the user lifts it (``mission unhalt`` needs an
  interactive terminal and the orchestrator guard denies it).
- A mission whose record has not changed for ``limits.stale_hours`` (default 24) is stale:
  ``mission list`` and ``doctor`` say so.
- ``notify.command`` in factory.json (an argv list, no shell) runs when a mission becomes
  BLOCKED and when the factory is halted, with SF_EVENT, SF_MISSION, SF_STATE and SF_REASON
  in its environment. It is best-effort: a failing or slow command never blocks the work.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime

from .core import FactoryError, load_config, now, safe_path

HALT = ".factory/local/HALT"
DEFAULT_STALE_HOURS = 24


def halted(root) -> dict | None:
    path = safe_path(root, HALT)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"reason": "HALT file present (unreadable)", "at": None}


def assert_not_halted(root, action: str) -> None:
    state = halted(root)
    if state:
        raise FactoryError(
            f"The factory is halted ({state.get('reason')}); {action} is refused until the user runs "
            "software-factory mission unhalt"
        )


def halt(root, reason: str) -> dict:
    if not isinstance(reason, str) or not reason.strip():
        raise FactoryError("Halt reason (--reason) requires concrete text")
    path = safe_path(root, HALT)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"reason": reason.strip(), "at": now(), "by": os.environ.get("USER")}
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    notify(root, "factory_halted", None, None, record["reason"])
    return {"halted": True, **record}


def unhalt(root) -> dict:
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        raise FactoryError(
            "mission unhalt lifts the kill switch, so only the user runs it, in an interactive terminal"
        )
    path = safe_path(root, HALT)
    if not path.is_file():
        return {"halted": False, "note": "The factory was not halted"}
    path.unlink()
    return {"halted": False}


def stale_hours(config: dict) -> int:
    value = (config.get("limits") or {}).get("stale_hours", DEFAULT_STALE_HOURS)
    return value if isinstance(value, int) and value > 0 else DEFAULT_STALE_HOURS


def idle_hours(updated_at: str | None) -> float | None:
    if not updated_at:
        return None
    try:
        then = datetime.fromisoformat(updated_at)
    except ValueError:
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=UTC)
    return round((datetime.now(UTC) - then).total_seconds() / 3600, 1)


def notify(root, event: str, mission: str | None, state: str | None, reason: str | None) -> None:
    try:
        config = load_config(root)
    except (FactoryError, OSError, ValueError):
        return
    command = (config.get("notify") or {}).get("command")
    if not command:
        return
    env = {
        **os.environ,
        "SF_EVENT": event,
        "SF_MISSION": mission or "",
        "SF_STATE": state or "",
        "SF_REASON": (reason or "")[:2000],
    }
    timeout = (config.get("notify") or {}).get("timeout_seconds", 10)
    try:
        subprocess.run(command, cwd=root, env=env, capture_output=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError):
        return
