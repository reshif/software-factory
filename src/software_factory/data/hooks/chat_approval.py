"""Record the user's own chat approvals (Claude Code UserPromptSubmit hook, pinned runtime).

Claude Code runs this hook with the text the human submitted, before the model sees it. When a
line of that text reads `approve <MISSION-ID> <scope|exception|merge|release>` (optionally
followed by a note), the hook records that approval itself through the factory's own
`approve_decision`, bound to what the approval covers (the current spec.md, the pending
constitution or the candidate, the CI-verified candidate, the release artifact). The agent
never records these decisions: `mission decision` refuses them and the orchestrator guard
denies `mission approve`. The hook tells the model what it recorded, or why it could not.

It is a local guardrail, not authentication: any process running as the user can edit the
mission records. It never blocks or rewrites the prompt: every path exits 0.

A line `approve P-0003 crew` (optionally followed by the proposal's hash prefix) saves that
project-knowledge proposal exactly as proposed, through `software-factory crew apply`.

Usage: .factory/.venv/bin/python -I -B chat_approval.py PROJECT_DIR   (hook JSON on stdin)
"""

from __future__ import annotations

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

MAX_PAYLOAD = 1 << 20
KINDS = ("scope", "exception", "merge", "release")
APPROVAL = re.compile(
    r"(?im)^[^\S\n]*approve[^\S\n]+([A-Za-z][A-Za-z0-9_-]{0,79})[^\S\n]+(" + "|".join(KINDS) + r")\b[^\n]*"
)


# `approve P-0003 crew [hash]`: save knowledge proposal P-0003 exactly as proposed (crew apply).
CREW_APPROVAL = re.compile(
    r"(?im)^[^\S\n]*approve[^\S\n]+(P-[0-9]{4,})[^\S\n]+crew\b(?:[^\S\n]+([0-9a-fA-F]{8,64})\b)?[^\n]*"
)


def record_crew(project: Path, prompt: str, session: str, at: str) -> list[str]:
    """Apply each knowledge proposal the user approved in their own message."""
    matches = list(CREW_APPROVAL.finditer(prompt))
    if not matches:
        return []
    from software_factory.core import FactoryError
    from software_factory.crew import apply

    notes = []
    for match in matches:
        proposal, pin, line = match.group(1), match.group(2), match.group(0).strip()[:300]
        try:
            saved = apply(
                project,
                proposal,
                via="chat",
                pin=pin,
                reference=f'Approved by the user in Claude Code chat (session {session}, {at}): "{line}"',
                confirm=lambda *_: None,
            )
            notes.append(
                f"software-factory saved knowledge proposal {proposal} ({saved['target']}, sha256 "
                f"{saved['sha256'][:8]}) from the user's chat message. Tell the user to commit it: "
                f"{saved.get('commit', '')}"
            )
        except (FactoryError, OSError, ValueError, KeyError) as exc:
            notes.append(
                f"software-factory could not save knowledge proposal {proposal}: {exc}. "
                "Tell the user; do not write knowledge yourself."
            )
    return notes


def record(project: Path, payload: dict) -> list[str]:
    """Record each approval line of the user's prompt; return notes for the model."""
    prompt = payload.get("prompt")
    if payload.get("hook_event_name") != "UserPromptSubmit" or not isinstance(prompt, str):
        return []
    session = payload.get("session_id") if isinstance(payload.get("session_id"), str) else "unknown"
    at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    notes = record_crew(project, prompt, session, at)
    matches = list(APPROVAL.finditer(prompt))
    if not matches:
        return notes
    from software_factory.core import FactoryError
    from software_factory.events import COMMAND
    from software_factory.workflow import approve_decision, load_mission

    COMMAND.set("chat approval")

    for match in matches:
        mission_id, kind, line = match.group(1), match.group(2).lower(), match.group(0).strip()[:300]
        try:
            mission = load_mission(project, mission_id)
            reference = (
                (mission.get("delivery") or {}).get("release_ref")
                if kind == "release"
                else f'Approved by the user in Claude Code chat (session {session}, {at}): "{line}"'
            )
            if not reference:
                raise FactoryError("Record the release reference first (record-delivery release_ref)")
            decision = approve_decision(project, mission_id, kind, reference, confirm=lambda *_: None)
            recorded = (
                decision["decisions"][-1] if isinstance(decision, dict) and decision.get("decisions") else {}
            )
            notes.append(
                f"software-factory recorded the user's {kind} approval for {mission_id} from their chat message "
                f"(decision {recorded.get('id', '?')}, bound to {recorded.get('subject_hash', '?')}). "
                + (
                    "Continue with `software-factory mission accept-scope`."
                    if kind == "scope"
                    else "Continue."
                )
            )
        except (FactoryError, OSError, ValueError, KeyError) as exc:
            notes.append(
                f"software-factory could not record the user's {kind} approval for {mission_id}: {exc}. "
                "Tell the user; do not record it yourself."
            )
    return notes


def main(argv=None, stdin=None, stdout=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    try:
        project = Path(argv[0]).resolve()
        if not (project / ".factory").is_dir():
            return 0
        payload = json.loads(stdin.read(MAX_PAYLOAD + 1)[:MAX_PAYLOAD])
        notes = record(project, payload) if isinstance(payload, dict) else []
    except Exception as exc:  # noqa: BLE001 - a hook failure must never block the user's prompt.
        notes = [
            f"software-factory's chat-approval hook failed ({type(exc).__name__}); nothing was recorded."
        ]
    if notes:
        context = {"hookEventName": "UserPromptSubmit", "additionalContext": " ".join(notes)}
        stdout.write(json.dumps({"hookSpecificOutput": context}) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
