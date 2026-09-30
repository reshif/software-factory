"""Export the win: a retro turns a finished mission's records into lessons the user approves.

From READY_PR on (including CANCELED), ``mission brief --kind retro`` gives the planner the
mission's *records only*: the event timeline, tasks and attempts, blockers, review findings
and resolutions, clarifications, decisions, the assessment's concerns and the options chosen.
It never includes the diff, check logs or source text, the main channels for hostile text.
The planner returns retro.md (recorded with ``record-doc --doc retro``) and a lessons JSON.

``crew propose --mission ID --input -`` checks every lesson: its type, a short plain text, and
evidence references that must resolve against the mission record. A ``default`` must quote an
answer the user actually gave in a clarification. Rules go to project.md; pitfalls, defaults and
criteria patterns go to a recipe. Each target becomes one proposal whose numbered items the user
approves one by one (``approve P-n crew 1,3``). ``preference`` lessons are shown for the user's
own me.md and ``control`` lessons (checks, hooks, roles, CI, policy) become a maintenance-mission
request: neither is ever written by the factory. Requests from a contributor or an anonymous
author produce no lessons.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

from .core import FactoryError, safe_path

LESSON_TYPES = ("rule", "pitfall", "default", "criteria-pattern", "preference", "control")
PROJECT_SECTIONS = ("Must not break", "Off-limits", "Rules", "Definition of done")
RECIPE_SECTION = {"pitfall": "Known pitfalls", "default": "Defaults", "criteria-pattern": "Criteria patterns"}
LESSON_FIELDS = {"id", "type", "text", "evidence", "section", "question", "answer"}
RECIPE_FIELDS = {"name", "lane", "trigger", "use_when"}
MAX_TEXT = 200
UNSAFE = re.compile(r"`|https?://|www\.|<|>|\|")
EVIDENCE = re.compile(
    r"(?:finding:(?P<review>[A-Za-z][A-Za-z0-9_-]*)/(?P<finding>[A-Za-z][A-Za-z0-9_-]*)"
    r"|clarification:(?P<clarification>[0-9]+)"
    r"|concern:(?P<concern>C-[0-9]+)"
    r"|task:(?P<task>[A-Za-z][A-Za-z0-9_-]*):attempt:(?P<attempt>[0-9]+)"
    r"|decision:(?P<decision>[A-Za-z][A-Za-z0-9_-]*)"
    r"|blocker:(?P<blocker>[0-9]+)"
    r"|event:(?P<event>[0-9]+))"
)


def retro_states():
    from .workflow import POST_MERGE_STATES

    return {"READY_PR", "CANCELED", *POST_MERGE_STATES}


def assert_retro_state(mission: dict) -> None:
    from .workflow import effective_state

    if effective_state(mission) not in retro_states() and mission["state"] not in retro_states():
        raise FactoryError(
            f"A retro looks back at finished work: {mission['id']} is {mission['state']}; run it from READY_PR on"
        )


def _events(root, mission_id: str) -> list[dict]:
    from .events import read_events

    try:
        return read_events(root, mission_id)
    except (ValueError, OSError):
        return []


def signals(root, mission_id: str) -> dict:
    """Deterministic reasons a retro is worth offering; the orchestrator offers, never runs it alone."""
    from .crew import deferred
    from .workflow import load_mission, mission_lane

    mission = load_mission(root, mission_id)
    found = []
    repaired = [t["id"] for t in mission["tasks"] if t.get("attempts", 0) > 1]
    if repaired:
        found.append(f"repairs: {', '.join(repaired)} needed more than one attempt")
    blocking = [
        f"{r['id']}/{f.get('id', '?')}"
        for r in mission.get("reviews", [])
        for f in r.get("findings", [])
        if f.get("severity") == "blocking"
    ]
    if blocking:
        found.append(f"blocking review findings: {', '.join(blocking)}")
    holds = len(mission.get("blockers", [])) + len(mission.get("resolved_blockers", []))
    if holds:
        found.append(f"holds: the mission was blocked or paused {holds} time(s)")
    events = _events(root, mission_id)
    accepted = next(
        (e["seq"] for e in events if e.get("command", "").startswith("mission accept-scope")), None
    )
    late = [
        e
        for e in events
        if accepted and e["seq"] > accepted and e.get("command", "").startswith("mission clarify")
    ]
    if late:
        found.append(f"course changes: {len(late)} clarification(s) after scope was accepted")
    plan = safe_path(root, f".factory/missions/{mission_id}/plan.md")
    if plan.is_file() and "(user override" in plan.read_text(encoding="utf-8", errors="replace"):
        found.append("the user chose another option than the grader's winner")
    if mission_lane(mission) == "feature" and not ((mission.get("crew") or {}).get("recipe")):
        found.append("no recipe exists for this kind of work yet: capture one")
    from .workflow import effective_state

    finished = mission["state"] in retro_states() or effective_state(mission) in retro_states()
    offered = finished and bool(found) and not deferred(root, f"retro:{mission_id}")
    return {
        "mission": mission_id,
        "signals": found,
        "offer": offered,
        "note": (
            "Offer the retro to the user once, in one line; run it only if they agree. "
            f'If they decline, record it with `crew defer --key retro:{mission_id} --for never --reference "..."`.'
        ),
    }


def render(root, mission: dict, lines: list[str], fenced, normalized, document) -> None:
    """Append the records-only retro brief body to ``lines``."""
    from .workflow import untrusted_source

    mission_id = mission["id"]
    lines.extend(["", "## Timeline (events.jsonl)", ""])
    for event in _events(root, mission_id):
        lines.append(f"- {event.get('seq')}: {normalized(str(event.get('command')))} → {event.get('state')}")
    lines.extend(["", "## Tasks", ""])
    for task in mission["tasks"]:
        extra = f"; blocked: {normalized(str(task['blocked_reason']))}" if task.get("blocked_reason") else ""
        lines.append(f"- {task['id']} ({task['status']}, {task.get('attempts', 0)} attempt(s)): "
                     f"{normalized(task['title'])}{extra}")  # fmt: skip
    lines.extend(["", "## Blockers", ""])
    holds = [*mission.get("resolved_blockers", []), *mission.get("blockers", [])]
    for number, hold in enumerate(holds, 1):
        text = (
            hold
            if isinstance(hold, str)
            else f"{hold.get('reason', '')} (resolution: {hold.get('resolution', 'open')})"
        )
        lines.append(f"- blocker:{number}: {normalized(str(text))[:300]}")
    if not holds:
        lines.append("- None.")
    lines.extend(["", "## Reviews", ""])
    for review in mission.get("reviews", []):
        lines.append(
            f"- {review['id']} ({review.get('kind', 'review')}, {review['status']}, by {normalized(review['author'])})"
        )
        for finding in review.get("findings", []):
            lines.append(
                f"  - finding:{review['id']}/{finding.get('id', '-')} [{finding['severity']}"
                f"{', verified' if finding.get('verified') else ''}] {normalized(finding['message'])[:300]}"
            )
        for resolution in review.get("resolutions", []):
            lines.append(
                f"  - resolved {resolution.get('finding')}: {normalized(str(resolution.get('reason')))[:300]}"
            )
    if not mission.get("reviews"):
        lines.append("- None.")
    lines.extend(["", "## Decisions", ""])
    for decision in mission["decisions"]:
        lines.append(
            f"- decision:{decision['id']} ({decision['kind']}): {normalized(decision['reference'])[:200]}"
        )
    if not mission["decisions"]:
        lines.append("- None.")
    source = untrusted_source(mission)
    if not source:
        clarifications = document("clarifications.md")
        lines.extend(["", "## Clarifications (the user's words; clarification:<n> in order)", "",
                      *fenced(clarifications or "None recorded.")])  # fmt: skip
    lines.extend(["", "## Assessment concerns", ""])
    assessment = document("assessment.md")
    match = re.search(r"^## Concerns\s*$(.*?)(?=^## |\Z)", assessment or "", re.MULTILINE | re.DOTALL)
    lines.extend(fenced((match.group(1).strip() if match else "") or "Not recorded."))
    lines.extend(
        [
            "",
            "## Saved knowledge (to avoid repeating it)",
            "",
            *fenced(document("crew-context.md") or "None."),
        ]
    )
    lines += ["", "## Return", ""]
    if source:
        lines += [
            (
                f"This request came from a {source}, so it produces no lessons (Learning with consent). Return only"
                " retro.md: what happened, what worked and what to change, from the records above."
            ),
        ]
        return
    recipe = ((mission.get("crew") or {}).get("recipe") or {}).get("name")
    lines += [
        (
            "Return two things: the text of retro.md (what happened, what worked, what to change, from the records"
            " above) and a lessons JSON for `crew propose --mission ID --input -`:"
        ),
        "",
        "```json",
        '{"recipe": {"name": "kebab-name", "lane": "feature", "trigger": ["words", "in", "requests"],',
        '            "use_when": "One line: when this recipe fits"},',
        ' "lessons": [{"id": "L-1", "type": "rule", "section": "Must not break", "text": "...",',
        '              "evidence": ["finding:V-code/F-2"]},',
        '             {"id": "L-2", "type": "default", "question": "Storage", "answer": "reuse the cache host",',
        '              "text": "Storage default", "evidence": ["clarification:2"]}]}',
        "```",
        "",
        "## Lesson rules",
        "",
        (
            "- Types: rule (project.md; section Must not break, Off-limits, Rules or Definition of done), pitfall,"
            " default and criteria-pattern (the recipe), preference (the user's own me.md, never written by the"
            " factory) and control (a check, hook, role, CI or policy change: becomes a maintenance-mission request)."
        ),
        (
            f"- Each text is one plain sentence of at most {MAX_TEXT} characters: no backticks, links, angle"
            " brackets or pipes."
        ),
        (
            "- Each lesson cites evidence from the records above: finding:<review>/<finding>, clarification:<n>,"
            " concern:C-<n>, task:<id>:attempt:<n>, decision:<id>, blocker:<n> or event:<seq>."
        ),
        "- A default quotes the user's own answer exactly as it appears in a clarification.",
        (
            f"- Recipe lessons extend recipe {recipe}; omit the recipe object."
            if recipe
            else "- Recipe lessons need the recipe object: a new recipe named for this kind of work."
        ),
        "- Keep only what would change the next mission; say what is not worth saving in retro.md.",
        "- Do not change files and do not start nested agents.",
    ]


def _resolve(root, mission: dict, ref: str) -> str | None:
    """Why an evidence reference does not resolve against the mission record, or None."""
    match = EVIDENCE.fullmatch(ref) if isinstance(ref, str) else None
    if not match:
        return f"unknown evidence reference {ref!r}"
    g = match.groupdict()
    if g["review"]:
        review = next((r for r in mission.get("reviews", []) if r["id"] == g["review"]), None)
        if not review or not any(f.get("id") == g["finding"] for f in review.get("findings", [])):
            return f"{ref} names no recorded finding"
    elif g["clarification"]:
        if not 1 <= int(g["clarification"]) <= len((mission.get("request") or {}).get("clarifications", [])):
            return f"{ref} names no recorded clarification"
    elif g["concern"]:
        text = safe_path(root, f".factory/missions/{mission['id']}/assessment.md")
        if not text.is_file() or not re.search(
            rf"\b{g['concern']}\b", text.read_text(encoding="utf-8", errors="replace")
        ):
            return f"{ref} names no concern in assessment.md"
    elif g["task"]:
        task = next((t for t in mission["tasks"] if t["id"] == g["task"]), None)
        if not task or not 1 <= int(g["attempt"]) <= task.get("attempts", 0):
            return f"{ref} names no recorded task attempt"
    elif g["decision"]:
        if not any(d["id"] == g["decision"] for d in mission["decisions"]):
            return f"{ref} names no recorded decision"
    elif g["blocker"]:
        if (
            not 1
            <= int(g["blocker"])
            <= len(mission.get("blockers", [])) + len(mission.get("resolved_blockers", []))
        ):
            return f"{ref} names no recorded blocker"
    elif g["event"] and not 1 <= int(g["event"]) <= len(_events(root, mission["id"])):
        return f"{ref} names no recorded event"
    return None


def _plain(text, label: str) -> str:
    if not isinstance(text, str) or not text.strip():
        raise FactoryError(f"{label} needs text")
    value = " ".join(text.split())
    if len(value) > MAX_TEXT:
        raise FactoryError(f"{label} is {len(value)} characters; the limit is {MAX_TEXT}")
    if UNSAFE.search(value) or value.startswith("#"):
        raise FactoryError(
            f"{label} must be plain text: no backticks, links, angle brackets, pipes or headings"
        )
    return value


def propose_lessons(root, mission_id: str, value: dict) -> dict:
    """Turn a retro's lessons into item proposals (one per target) for the user to approve one by one."""
    from .core import now
    from .crew import (
        RECIPE_NAME,
        _next_id,
        _proposal_file,
        _proposal_hash,
        _read,
        _sha,
        compose,
        target_path,
        validate_text,
    )
    from .workflow import _normalized, load_mission, request_texts, untrusted_source

    mission = load_mission(root, mission_id)
    assert_retro_state(mission)
    source = untrusted_source(mission)
    if source:
        raise FactoryError(f"A request from a {source} is untrusted input: it produces no lessons")
    if not isinstance(value, dict) or not isinstance(value.get("lessons"), list) or not value["lessons"]:
        raise FactoryError('Lessons input is {"lessons": [...], "recipe": {...}} with at least one lesson')
    unknown = set(value) - {"lessons", "recipe"}
    if unknown:
        raise FactoryError(f"Unknown lessons field(s): {', '.join(sorted(unknown))}")
    texts = [_normalized(t) for t in request_texts(root, mission)[1:]]
    used = ((mission.get("crew") or {}).get("recipe") or {}).get("name")
    meta = value.get("recipe")
    items, preferences, controls, seen = {}, [], [], set()
    stamp = f"{mission_id}, {datetime.now(UTC).date().isoformat()}"
    for lesson in value["lessons"]:
        if not isinstance(lesson, dict):
            raise FactoryError("Each lesson is an object")
        extra = set(lesson) - LESSON_FIELDS
        if extra:
            raise FactoryError(f"Lesson has unknown field(s): {', '.join(sorted(extra))}")
        lid = lesson.get("id")
        if not isinstance(lid, str) or not re.fullmatch(r"L-[0-9]{1,4}", lid) or lid in seen:
            raise FactoryError(f"Lesson ids are unique L-<n>; got {lid!r}")
        seen.add(lid)
        kind = lesson.get("type")
        if kind not in LESSON_TYPES:
            raise FactoryError(f"{lid} type must be one of {', '.join(LESSON_TYPES)}")
        text = _plain(lesson.get("text"), f"{lid} text")
        evidence = lesson.get("evidence")
        if not isinstance(evidence, list) or not evidence:
            raise FactoryError(f"{lid} needs evidence from the mission record")
        problems = [p for p in (_resolve(root, mission, ref) for ref in evidence) if p]
        if problems:
            raise FactoryError(f"{lid}: " + "; ".join(problems))
        if kind == "preference":
            preferences.append({"lesson": lid, "text": text})
            continue
        if kind == "control":
            controls.append(
                {
                    "lesson": lid,
                    "request": f"{text} (lesson {lid} from {mission_id}; evidence: {', '.join(evidence)})",
                }
            )
            continue
        if kind == "rule":
            section = lesson.get("section") or "Rules"
            if section not in PROJECT_SECTIONS:
                raise FactoryError(f"{lid} section must be one of {', '.join(PROJECT_SECTIONS)}")
            target, line = "project", f"- {text} ({stamp})"
        else:
            name = used or (meta or {}).get("name")
            if not isinstance(name, str) or not RECIPE_NAME.fullmatch(name):
                raise FactoryError(f"{lid} is a recipe lesson: give the recipe object with a kebab-case name")
            target, section = f"recipe:{name}", RECIPE_SECTION[kind]
            if kind == "default":
                question = _plain(lesson.get("question"), f"{lid} question")
                answer = _plain(lesson.get("answer"), f"{lid} answer")
                if not any(_normalized(answer) in t for t in texts):
                    raise FactoryError(
                        f"{lid} default must quote the user's answer exactly as a clarification records it"
                    )
                line = f"| {question} | {answer} ({stamp}) |"
            else:
                line = f"- {text} ({stamp})"
        items.setdefault(target, []).append(
            {"section": section, "line": line, "lesson": lid, "evidence": evidence}
        )
    proposals = []
    for target, entries in sorted(items.items()):
        current = _read(target_path(root, target))
        if current is not None:
            base_text = None
        elif target == "project":
            base_text = "# Project\n\n" + "\n\n".join(f"## {s}" for s in ("Purpose", "Users", "Must not break",
                        "Definition of done", "Off-limits", "Rules")) + "\n"  # fmt: skip
        else:
            name = target[7:]
            if not isinstance(meta, dict) or set(meta) - RECIPE_FIELDS or meta.get("name") != name:
                raise FactoryError(
                    f"Recipe {name} does not exist yet: give the recipe object (name, lane, trigger, use_when)"
                )
            trigger = meta.get("trigger") or []
            if not isinstance(trigger, list) or not all(isinstance(t, str) and t.strip() for t in trigger):
                raise FactoryError("recipe.trigger is a list of words")
            base_text = (
                f"---\nname: {name}\nlane: {_plain(meta.get('lane') or mission['kind'], 'recipe lane')}\n"
                f"trigger: {json.dumps([_plain(t, 'recipe trigger') for t in trigger])}\n"
                f"created: {datetime.now(UTC).date().isoformat()}\nupdated: {datetime.now(UTC).date().isoformat()}\n"
                f'source_missions: ["{mission_id}"]\n---\n## Use when\n{_plain(meta.get("use_when"), "recipe use_when")}\n\n'
                "## Defaults\n| Question | Default |\n| --- | --- |\n\n## Criteria patterns\n\n## Known pitfalls\n"
            )
        numbered = [{"n": n, **entry} for n, entry in enumerate(entries, 1)]
        full = compose(current.decode("utf-8") if current is not None else base_text, numbered)
        validate_text(target, full)
        proposal = {
            "id": _next_id(root),
            "target": target,
            "text": full,
            "items": numbered,
            "base_sha256": _sha(current),
            "base_text": base_text,
            "origin": f"retro:{mission_id}",
            "status": "proposed",
            "created_at": now(),
        }
        proposal["sha256"] = _proposal_hash(proposal)
        with open(_proposal_file(root, proposal["id"]), "x", encoding="utf-8") as handle:
            handle.write(json.dumps(proposal, indent=2, ensure_ascii=False) + "\n")
        proposals.append({
            "proposal": proposal["id"],
            "target": target,
            "sha256": proposal["sha256"],
            "items": [{"n": i["n"], "lesson": i["lesson"], "line": i["line"], "evidence": i["evidence"]} for i in numbered],
            "approve": f"After the mission merges, the user replies `approve {proposal['id']} crew "
            f"{','.join(str(i['n']) for i in numbered)}` (or a subset) or runs `crew apply --proposal "
            f"{proposal['id']} --items ...` in their terminal.",
        })  # fmt: skip
    return {
        "mission": mission_id,
        "proposals": proposals,
        "for_your_me_md": preferences,
        "maintenance_requests": controls,
        "note": "Show the user every item with its evidence; nothing is saved until they approve items by number.",
    }
