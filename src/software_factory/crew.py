"""Project knowledge the factory learns with the user's consent (Crew, constitution *Learning with consent*).

Layers:

- ``.factory/crew/project.md``: purpose, users, what must not break, definition of done,
  off-limits and rules for this repository. Committed and protected.
- ``.factory/crew/recipes/<name>.md``: a repeatable kind of mission: when to use it, the
  answers the user gave last time (defaults), criteria patterns and known pitfalls. Data only:
  a recipe is never a skill and grants no tool.
- ``$XDG_CONFIG_HOME/software-factory/me.md``: the user's personal profile, outside every
  repository (``SOFTWARE_FACTORY_ME`` overrides the path).

Agents only *propose*: ``crew propose`` validates text and stores an inert proposal under
``.factory/local/crew/proposals``. Only the user *applies*: ``crew apply`` in an interactive
terminal with the proposal ID typed back, or the chat hook from the user's own line
``approve P-0001 crew``. Every repository apply appends to the hash-chained
``.factory/crew/ledger.jsonl``; ``crew status`` reports a broken chain and knowledge files
changed outside it. Like mission records this is tamper-evident, not tamper-proof.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

from .core import FactoryError, now, safe_path, write_bytes

CREW_DIR = ".factory/crew"
PROJECT = f"{CREW_DIR}/project.md"
RECIPES = f"{CREW_DIR}/recipes"
LEDGER = f"{CREW_DIR}/ledger.jsonl"
PRIVATE = ".factory/local/crew"
PROPOSALS = f"{PRIVATE}/proposals"
DEFERRALS = f"{PRIVATE}/deferrals.json"

LIMITS = {"project": 8 * 1024, "recipe": 4 * 1024, "personal": 4 * 1024}
PROJECT_SECTIONS = ("Purpose", "Users", "Must not break", "Definition of done", "Off-limits", "Rules")
RECIPE_SECTIONS = ("Use when", "Defaults", "Criteria patterns", "Known pitfalls")
RECIPE_KEYS = {"name", "lane", "trigger", "created", "updated", "source_missions"}
RECIPE_NAME = re.compile(r"[a-z][a-z0-9-]{0,39}")
PROPOSAL_ID = re.compile(r"P-[0-9]{4,}")
DEFER_KEY = re.compile(r"(?:project|personal|recipe:[a-z][a-z0-9-]{0,39}|retro:[A-Za-z][A-Za-z0-9_-]{0,79})")
NOT_NOW_DAYS = 14
# A line that the chat hook would read as an approval: knowledge must never carry one, or
# pasting it back into chat could approve something.
APPROVAL_LINE = re.compile(
    r"(?im)^[^\S\n]*approve[^\S\n]+\S+[^\S\n]+(?:scope|exception|merge|release|crew)\b"
)
INVISIBLE = {"Cf", "Co", "Cs"}  # format (bidi, zero-width), private use, surrogates


def personal_path() -> Path:
    override = os.environ.get("SOFTWARE_FACTORY_ME")
    if override:
        return Path(override).expanduser()
    base = os.environ.get("XDG_CONFIG_HOME") or (os.environ.get("APPDATA") if os.name == "nt" else None)
    return Path(base or Path.home() / ".config") / "software-factory" / "me.md"


def _sha(data: bytes | None) -> str | None:
    return hashlib.sha256(data).hexdigest() if data is not None else None


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def parse_target(target: str) -> tuple[str, str | None]:
    if target in ("project", "personal"):
        return target, None
    if isinstance(target, str) and target.startswith("recipe:") and RECIPE_NAME.fullmatch(target[7:]):
        return "recipe", target[7:]
    raise FactoryError(
        "Knowledge target must be project, personal or recipe:<name> (lowercase letters, digits, hyphens)"
    )


def target_path(root, target: str) -> Path:
    kind, name = parse_target(target)
    if kind == "personal":
        return personal_path()
    return safe_path(root, PROJECT if kind == "project" else f"{RECIPES}/{name}.md")


def _read(path: Path) -> bytes | None:
    if path.is_symlink():
        raise FactoryError(f"Knowledge file is a symlink: {path}")
    return path.read_bytes() if path.is_file() else None


def _headings(text: str) -> list[str]:
    return [line[3:].strip() for line in text.splitlines() if line.startswith("## ")]


def _front_matter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        raise FactoryError("A recipe starts with front matter between --- lines (name, lane, trigger, …)")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise FactoryError("Recipe front matter is not closed with a --- line")
    fields = {}
    for line in text[4:end].splitlines():
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if not sep or key not in RECIPE_KEYS:
            raise FactoryError(
                f"Recipe front matter allows only {', '.join(sorted(RECIPE_KEYS))}; got {key or line!r}"
                " (a recipe is data: it cannot grant tools)"
            )
        if value.startswith("["):
            try:
                value = json.loads(value)
            except ValueError as exc:
                raise FactoryError(f"Recipe {key} must be a JSON list of strings") from exc
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                raise FactoryError(f"Recipe {key} must be a JSON list of strings")
        fields[key] = value
    return fields, text[end + 5 :]


def validate_text(target: str, text: str) -> str:
    """Refuse knowledge text that could hide instructions, leak secrets or smuggle approvals."""
    from .workflow import refuse_secrets

    kind, name = parse_target(target)
    if not isinstance(text, str) or not text.strip():
        raise FactoryError("Knowledge text is empty")
    text = text.replace("\r\n", "\n")
    if not text.endswith("\n"):
        text += "\n"
    size = len(text.encode())
    if size > LIMITS[kind]:
        raise FactoryError(
            f"{target} knowledge is {size} bytes; the limit is {LIMITS[kind]} (consolidate it; nothing is "
            "truncated)"
        )
    for char in text:
        if char not in "\n\t" and (
            unicodedata.category(char) in INVISIBLE or unicodedata.category(char) == "Cc"
        ):
            raise FactoryError(
                f"Knowledge text contains an invisible or control character (U+{ord(char):04X}); "
                "write plain text"
            )
    if "<!--" in text:
        raise FactoryError("Knowledge text may not contain HTML comments (text a reader cannot see)")
    if APPROVAL_LINE.search(text):
        raise FactoryError(
            "Knowledge text contains a line that reads as an approval (approve <ID> <kind>); remove it"
        )
    refuse_secrets(text, f"{target} knowledge")
    if kind == "project":
        missing = [s for s in PROJECT_SECTIONS if s not in _headings(text)]
        if missing:
            raise FactoryError(f"project.md needs the sections: {', '.join('## ' + s for s in missing)}")
    if kind == "recipe":
        fields, body = _front_matter(text)
        if fields.get("name") != name:
            raise FactoryError(f"Recipe front matter name must be {name}")
        missing = [s for s in RECIPE_SECTIONS if s not in _headings(body)]
        if missing:
            raise FactoryError(f"A recipe needs the sections: {', '.join('## ' + s for s in missing)}")
    return text


# --- proposals ---------------------------------------------------------------------------


def _private(root) -> Path:
    from .calibration import assert_private_directory

    assert_private_directory(root, PRIVATE)
    return safe_path(root, PROPOSALS)


def _proposal_file(root, proposal_id: str) -> Path:
    if not isinstance(proposal_id, str) or not PROPOSAL_ID.fullmatch(proposal_id):
        raise FactoryError(f"Invalid proposal ID: {proposal_id!r} (expected P-0001)")
    return safe_path(root, f"{PROPOSALS}/{proposal_id}.json")


def _proposal_hash(proposal: dict) -> str:
    body = {k: proposal[k] for k in ("id", "target", "text", "base_sha256", "origin")}
    return hashlib.sha256(_canonical(body).encode()).hexdigest()


def load_proposal(root, proposal_id: str) -> dict:
    path = _proposal_file(root, proposal_id)
    if not path.is_file():
        raise FactoryError(f"No knowledge proposal {proposal_id}")
    proposal = json.loads(path.read_text(encoding="utf-8"))
    if _proposal_hash(proposal) != proposal.get("sha256"):
        raise FactoryError(f"Proposal {proposal_id} was changed after it was proposed; propose it again")
    return proposal


def list_proposals(root) -> list[dict]:
    directory = safe_path(root, PROPOSALS)
    found = []
    for path in sorted(directory.glob("P-*.json")) if directory.is_dir() else []:
        try:
            proposal = load_proposal(root, path.stem)
        except (FactoryError, ValueError, OSError) as exc:
            found.append({"id": path.stem, "status": "invalid", "error": str(exc)})
            continue
        found.append(
            {k: proposal.get(k) for k in ("id", "target", "status", "created_at", "sha256", "origin")}
        )
    return found


def _next_id(root) -> str:
    directory = _private(root)
    directory.mkdir(parents=True, exist_ok=True)
    numbers = [int(p.stem[2:]) for p in directory.glob("P-*.json") if PROPOSAL_ID.fullmatch(p.stem)]
    ledger_numbers = [
        int(e["proposal"][2:]) for e in read_ledger(root) if PROPOSAL_ID.fullmatch(str(e.get("proposal", "")))
    ]
    return f"P-{max(numbers + ledger_numbers + [0]) + 1:04d}"


def propose(root, target: str, text: str, origin: str | None = None) -> dict:
    """Store an inert proposal; nothing is written to knowledge until the user applies it."""
    text = validate_text(target, text)
    current = _read(target_path(root, target))
    if current is not None and current.decode("utf-8", "replace") == text:
        raise FactoryError(f"{target} knowledge already has exactly this text")
    proposal = {
        "id": _next_id(root),
        "target": target,
        "text": text,
        "base_sha256": _sha(current),
        "origin": origin or "agent",
        "status": "proposed",
        "created_at": now(),
    }
    proposal["sha256"] = _proposal_hash(proposal)
    path = _proposal_file(root, proposal["id"])
    with open(path, "x", encoding="utf-8") as handle:  # never overwrite: IDs are not reused
        handle.write(json.dumps(proposal, indent=2, ensure_ascii=False) + "\n")
    return {
        "proposal": proposal["id"],
        "target": target,
        "sha256": proposal["sha256"],
        "replaces": proposal["base_sha256"],
        "text": text,
        "approve": (
            f"Show the user this exact text. To save it they reply in chat `approve {proposal['id']} crew`, "
            f"or run `software-factory crew apply --proposal {proposal['id']}` in their terminal."
            if target != "personal"
            else "Show the user this exact text. Personal knowledge is saved only from their own terminal: "
            f"`software-factory crew apply --proposal {proposal['id']}`."
        ),
    }


def _set_status(root, proposal: dict, status: str, **extra) -> None:
    updated = {**proposal, "status": status, **extra}
    write_bytes(
        root,
        f"{PROPOSALS}/{proposal['id']}.json",
        (json.dumps(updated, indent=2, ensure_ascii=False) + "\n").encode(),
    )


def withdraw(root, proposal_id: str) -> dict:
    proposal = load_proposal(root, proposal_id)
    if proposal["status"] != "proposed":
        raise FactoryError(f"Proposal {proposal_id} is {proposal['status']}, not proposed")
    _set_status(root, proposal, "withdrawn", withdrawn_at=now())
    return {"proposal": proposal_id, "status": "withdrawn"}


# --- ledger ------------------------------------------------------------------------------


def read_ledger(root) -> list[dict]:
    path = safe_path(root, LEDGER)
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def verify_ledger(root) -> dict:
    try:
        entries = read_ledger(root)
    except ValueError as exc:
        return {"count": 0, "problems": [f"ledger.jsonl is not valid JSON lines ({exc})"]}
    problems, previous = [], None
    for index, entry in enumerate(entries, 1):
        body = {k: v for k, v in entry.items() if k != "hash"}
        if entry.get("seq") != index:
            problems.append(
                f"ledger entry {index} has sequence {entry.get('seq')}; one was removed or reordered"
            )
        if hashlib.sha256(_canonical(body).encode()).hexdigest() != entry.get("hash"):
            problems.append(f"ledger entry {index} was altered")
        if entry.get("prev") != (previous["hash"] if previous else None):
            problems.append(f"ledger entry {index} does not follow entry {index - 1}")
        previous = entry
    return {"count": len(entries), "problems": problems}


def _append_ledger(root, entry: dict) -> dict:
    from .events import _actor

    entries = read_ledger(root)
    last = entries[-1] if entries else None
    record = {
        "seq": (last["seq"] + 1) if last else 1,
        "at": now(),
        "actor": _actor(),
        **entry,
        "prev": last["hash"] if last else None,
    }
    record["hash"] = hashlib.sha256(_canonical(record).encode()).hexdigest()
    path = safe_path(root, LEDGER)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(_canonical(record) + "\n")
    return record


def _last_applied(entries: list[dict]) -> dict:
    latest = {}
    for entry in entries:
        latest[entry.get("target")] = entry.get("new_sha256")
    return latest


# --- apply -------------------------------------------------------------------------------


def _open_product_missions(root) -> list[str]:
    from .workflow import PRE_MERGE_STATES, effective_state, list_missions, load_mission

    open_ids = []
    for item in list_missions(root)["missions"]:
        if "error" in item:
            continue
        mission = load_mission(root, item["id"])
        # A PROPOSED mission picks new knowledge up with `crew refresh`, which moves its base past it.
        state = effective_state(mission)
        if mission["kind"] != "maintenance" and state in PRE_MERGE_STATES and state != "PROPOSED":
            open_ids.append(mission["id"])
    return open_ids


def _confirm_on_terminal(proposal: dict) -> None:
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        raise FactoryError(
            "crew apply saves knowledge the user approved, so it only runs in an interactive terminal; "
            "an agent or script cannot run it"
            + (
                ""
                if proposal["target"] == "personal"
                else f" (in Claude Code the user can reply `approve {proposal['id']} crew` instead)"
            )
        )
    sys.stderr.write(
        f"Knowledge proposal {proposal['id']} for {proposal['target']}"
        + (f" (replaces sha256 {proposal['base_sha256']})" if proposal["base_sha256"] else " (new)")
        + ":\n\n"
        + proposal["text"]
        + f"\nType the proposal ID ({proposal['id']}) to save exactly this text: "
    )
    sys.stderr.flush()
    if sys.stdin.readline().strip() != proposal["id"]:
        raise FactoryError("Knowledge not saved: the typed proposal ID did not match")


def apply(root, proposal_id: str, *, via: str = "terminal", reference: str | None = None, pin: str | None = None,
          confirm=None) -> dict:  # fmt: skip
    """Save an approved proposal. ``via`` is "terminal" (typed confirmation) or "chat" (the hook)."""
    from .workflow import state_lock

    proposal = load_proposal(root, proposal_id)
    if proposal["status"] != "proposed":
        raise FactoryError(
            f"Proposal {proposal_id} is {proposal['status']}; propose again to change knowledge"
        )
    if pin and not proposal["sha256"].startswith(pin.lower()):
        raise FactoryError(
            f"Proposal {proposal_id} is not the one approved (hash {proposal['sha256'][:8]}, not {pin})"
        )
    target = proposal["target"]
    kind, _ = parse_target(target)
    if kind == "personal" and via != "terminal":
        raise FactoryError("Personal knowledge (me.md) is saved only from the user's own terminal")
    validate_text(target, proposal["text"])  # rules may have tightened since it was proposed
    path = target_path(root, target)
    current = _read(path)
    if _sha(current) != proposal["base_sha256"]:
        raise FactoryError(
            f"{target} knowledge changed since {proposal_id} was proposed; propose it again from the current text"
        )
    if kind != "personal":
        blocking = _open_product_missions(root)
        if blocking:
            raise FactoryError(
                "Saving project knowledge now would put a protected path into the candidate of open product "
                f"mission(s) {', '.join(blocking)}. Apply it from a checkout with no pre-merge product mission "
                "(for example the main worktree while missions run in their own worktrees), or after they merge"
            )
    (confirm or _confirm_on_terminal)(proposal)
    data = proposal["text"].encode()
    with state_lock(root):
        if kind == "personal":
            path.parent.mkdir(parents=True, exist_ok=True)
            if current is not None:
                previous = path.with_name(path.name + ".prev")
                previous.write_bytes(current)
                previous.chmod(0o600)
            temporary = path.with_name(f".{path.name}.tmp")
            temporary.write_bytes(data)
            temporary.chmod(0o600)
            os.replace(temporary, path)
            entry = None
        else:
            write_bytes(root, path.relative_to(Path(root).resolve()).as_posix(), data, mode=0o644)
            entry = _append_ledger(
                root,
                {
                    "action": "apply",
                    "proposal": proposal_id,
                    "proposal_sha256": proposal["sha256"],
                    "target": target,
                    "base_sha256": proposal["base_sha256"],
                    "new_sha256": _sha(data),
                    "origin": proposal["origin"],
                    "via": via,
                    "reference": reference
                    or ("typed confirmation in the user's terminal" if via == "terminal" else ""),
                },
            )
        _set_status(root, proposal, "applied", applied_at=now(), via=via)
    result = {"applied": proposal_id, "target": target, "path": str(path), "sha256": _sha(data)}
    if entry:
        result["ledger_seq"] = entry["seq"]
        result["commit"] = f'git add {CREW_DIR} && git commit -m "crew: {target} knowledge ({proposal_id})"'
    return result


def forget(root, target: str, reference: str | None = None, confirm=None) -> dict:
    """Remove a knowledge file (the user only); the ledger keeps a tombstone."""
    from .workflow import state_lock

    kind, _ = parse_target(target)
    path = target_path(root, target)
    current = _read(path)
    if current is None:
        raise FactoryError(f"No {target} knowledge to forget")
    if confirm is None:
        if not (sys.stdin.isatty() and sys.stderr.isatty()):
            raise FactoryError(
                "crew forget removes knowledge, so only the user runs it, in an interactive terminal"
            )
        sys.stderr.write(f"Type {target} to remove {path}: ")
        sys.stderr.flush()
        if sys.stdin.readline().strip() != target:
            raise FactoryError("Nothing removed: the typed target did not match")
    else:
        confirm(target)
    with state_lock(root):
        path.unlink()
        if kind != "personal":
            _append_ledger(
                root,
                {"action": "forget", "target": target, "base_sha256": _sha(current), "new_sha256": None,
                 "via": "terminal", "reference": reference or "typed confirmation in the user's terminal"},
            )  # fmt: skip
    return {"forgotten": target, "path": str(path)}


# --- deferrals ---------------------------------------------------------------------------


def _deferrals(root) -> dict:
    path = safe_path(root, DEFERRALS)
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    except (OSError, ValueError):
        return {}


def defer(root, key: str, kind: str | None, reference: str | None, clear: bool = False) -> dict:
    if not isinstance(key, str) or not DEFER_KEY.fullmatch(key):
        raise FactoryError("Deferral key must be project, personal, recipe:<name> or retro:<MISSION>")
    _private(root)
    records = _deferrals(root)
    if clear:
        records.pop(key, None)
    else:
        if kind not in ("not-now", "never"):
            raise FactoryError("--for must be not-now or never")
        if not isinstance(reference, str) or not reference.strip():
            raise FactoryError("--reference needs the user's own words (a deferral records their choice)")
        records[key] = {"for": kind, "at": now(), "reference": reference.strip()[:500]}
    write_bytes(root, DEFERRALS, (json.dumps(records, indent=2, sort_keys=True) + "\n").encode())
    return {"key": key, "deferral": records.get(key)}


def deferred(root, key: str) -> bool:
    from datetime import UTC, datetime

    record = _deferrals(root).get(key)
    if not record:
        return False
    if record.get("for") == "never":
        return True
    try:
        at = datetime.fromisoformat(str(record.get("at")))
    except ValueError:
        return False
    return (datetime.now(UTC) - at).days < NOT_NOW_DAYS


# --- reading -----------------------------------------------------------------------------


def _recipe_summary(root, path: Path, latest: dict) -> dict:
    data = path.read_bytes()
    item = {
        "name": path.stem,
        "sha256": _sha(data),
        "ledgered": latest.get(f"recipe:{path.stem}") == _sha(data),
    }
    try:
        fields, _ = _front_matter(data.decode("utf-8"))
        item.update({k: fields.get(k) for k in ("lane", "trigger", "updated", "source_missions")})
    except (FactoryError, UnicodeDecodeError) as exc:
        item["error"] = str(exc)
    return item


def library(root) -> dict:
    latest = _last_applied(read_ledger(root))
    directory = safe_path(root, RECIPES)
    recipes = (
        [_recipe_summary(root, p, latest) for p in sorted(directory.glob("*.md"))]
        if directory.is_dir()
        else []
    )
    return {"recipes": recipes}


def match(root, text: str) -> dict:
    """Recipes whose trigger words appear in the text; the orchestrator suggests, the user confirms."""
    words = (text or "").lower()
    found = []
    for recipe in library(root)["recipes"]:
        hits = [t for t in recipe.get("trigger") or [] if isinstance(t, str) and t.lower() in words]
        if hits:
            found.append({"name": recipe["name"], "lane": recipe.get("lane"), "matched": hits})
    found.sort(key=lambda r: -len(r["matched"]))
    return {
        "matches": found,
        "note": "Suggest the best match to the user and use it only if they confirm "
        "(mission create --recipe NAME, or mission recipe --use NAME while PROPOSED)",
    }


def show(root, target: str) -> dict:
    path = target_path(root, target)
    data = _read(path)
    if data is None:
        raise FactoryError(f"No {target} knowledge recorded")
    history = [e for e in read_ledger(root) if e.get("target") == target]
    return {"target": target, "path": str(path), "sha256": _sha(data), "text": data.decode("utf-8", "replace"),
            "ledger": history}  # fmt: skip


def status(root) -> dict:
    latest = _last_applied(read_ledger(root))
    project = _read(safe_path(root, PROJECT))
    try:
        me = _read(personal_path())
    except OSError:
        me = None
    report = {
        "project": {
            "present": project is not None,
            "sha256": _sha(project),
            "ledgered": project is None or latest.get("project") == _sha(project),
        },
        "personal": {"path": str(personal_path()), "present": me is not None, "sha256": _sha(me)},
        **library(root),
        "ledger": verify_ledger(root),
        "proposals": [p for p in list_proposals(root) if p.get("status") in ("proposed", "invalid")],
        "deferrals": _deferrals(root),
    }
    gaps = []
    if project is None and not deferred(root, "project"):
        gaps.append(
            "No project knowledge yet: offer once, in one line, to draft .factory/crew/project.md with the user "
            "(crew propose --target project); never block the task"
        )
    if me is None and not deferred(root, "personal"):
        gaps.append(
            "No personal profile: offer once, in one line, to draft one with the user (crew propose --target "
            "personal); never block the task"
        )
    if not report["project"]["ledgered"]:
        gaps.append(".factory/crew/project.md changed outside crew apply since the last approved version")
    gaps += [f"Recipe {r['name']} changed outside crew apply" for r in report["recipes"] if not r["ledgered"]]
    gaps += [f"Ledger: {p}" for p in report["ledger"]["problems"]]
    gaps += [f"Proposal {p['id']} awaits the user's approval ({p.get('target')})" for p in report["proposals"]
             if p.get("status") == "proposed"]  # fmt: skip
    report["gaps"] = gaps
    return report


# --- CLI ---------------------------------------------------------------------------------


def _input_text(root, source: str) -> str:
    from .workflow import read_text_input

    return read_text_input(root, source, "Knowledge --input")[1]


def handle(args) -> dict:
    root, command = args.root, args.crew_command
    if command == "status":
        return status(root)
    if command == "library":
        return library(root)
    if command == "show":
        return show(root, args.target)
    if command == "propose":
        return propose(root, args.target, _input_text(root, args.input))
    if command == "proposals":
        return {"proposals": list_proposals(root)}
    if command == "withdraw":
        return withdraw(root, args.proposal)
    if command == "defer":
        return defer(root, args.key, args.for_, args.reference, clear=args.clear)
    if command == "apply":
        return apply(root, args.proposal, pin=args.hash)
    if command == "forget":
        return forget(root, args.target)
    if command == "refresh":
        return refresh(root, args.mission)
    if command == "match":
        return match(root, args.text)
    raise FactoryError(f"Unknown crew command: {command}")


def add_parser(subparsers) -> None:
    crew = subparsers.add_parser("crew", help="Project knowledge kept with your consent")
    actions = crew.add_subparsers(dest="crew_command", required=True, metavar="<action>")
    actions.add_parser("status", help="Knowledge present, its hashes, the ledger, pending proposals and gaps")
    actions.add_parser("library", help="List recipes (repeatable kinds of mission)")
    p = actions.add_parser("show", help="Print one knowledge file and its ledger history")
    p.add_argument("--target", required=True, help="project, personal or recipe:<name>")
    p = actions.add_parser("propose", help="Store an inert knowledge proposal for the user to approve")
    p.add_argument("--target", required=True, help="project, personal or recipe:<name>")
    p.add_argument(
        "--input", required=True, help="Repository-relative file with the full new text, or - for stdin"
    )
    actions.add_parser("proposals", help="List knowledge proposals")
    p = actions.add_parser("withdraw", help="Withdraw a proposal that has not been applied")
    p.add_argument("--proposal", required=True, metavar="P-0001", help="Proposal ID")
    p = actions.add_parser("defer", help="Record the user's 'not now' (14 days) or 'never' for an offer")
    p.add_argument("--key", required=True, help="project, personal, recipe:<name> or retro:<MISSION>")
    p.add_argument("--for", dest="for_", choices=("not-now", "never"), help="not-now (14 days) or never")
    p.add_argument("--reference", help="The user's own words")
    p.add_argument("--clear", action="store_true", help="Remove the deferral")
    p = actions.add_parser("apply", help="Save a proposal you approved (you, in an interactive terminal)")
    p.add_argument("--proposal", required=True, metavar="P-0001", help="Proposal ID")
    p.add_argument(
        "--hash", help="Optional: the first 8+ hex of the proposal's sha256, to pin what you approved"
    )
    p = actions.add_parser("forget", help="Remove a knowledge file (you, in an interactive terminal)")
    p.add_argument("--target", required=True, help="project, personal or recipe:<name>")
    p = actions.add_parser("match", help="Recipes whose triggers appear in a request text")
    p.add_argument("--text", required=True, help="The request text, or a short summary of it")
    p = actions.add_parser(
        "refresh", help="Re-freeze a PROPOSED mission's saved knowledge after you saved new knowledge"
    )
    p.add_argument("--mission", required=True, metavar="ID", help="Mission ID")
    for parser in actions.choices.values():
        parser.set_defaults(handler=handle)


# --- missions: frozen knowledge ----------------------------------------------------------

CONTEXT_DOC = "crew-context.md"
PLANNER_KINDS = ("context", "research", "assess", "spec", "options", "plan")


def settings(config: dict) -> dict:
    crew = config.get("crew") or {}
    return {"enabled": crew.get("enabled", True), "personal": crew.get("personal", True)}


def personal_snapshot_path(mission_id: str) -> str:
    return f"{PRIVATE}/me-{mission_id}.md"


def _demote(text: str) -> str:
    return "\n".join("#" + line if line.startswith("#") else line for line in text.rstrip("\n").splitlines())


def load_recipe(root, name: str) -> bytes:
    """A recipe's exact bytes, refused unless it still passes the knowledge rules."""
    target = f"recipe:{name}"
    data = _read(target_path(root, target))
    if data is None:
        known = ", ".join(r["name"] for r in library(root)["recipes"]) or "none"
        raise FactoryError(f"No recipe {name} in .factory/crew/recipes (known: {known})")
    validate_text(target, data.decode("utf-8", "replace"))
    return data


def render_context(mission_id: str, project: bytes | None, recipe: tuple[str, bytes] | None = None) -> str:
    """The deterministic, committed crew-context.md: project knowledge only, never personal text."""
    lines = [f"# Saved knowledge: {mission_id}", ""]
    if project is None:
        lines += ["No project knowledge was recorded in .factory/crew/project.md when this was frozen."]
    else:
        lines += [
            f"## Project knowledge (.factory/crew/project.md, sha256 {_sha(project)})",
            "",
            _demote(project.decode("utf-8", "replace")),
        ]
    if recipe:
        name, data = recipe
        _, body = _front_matter(data.decode("utf-8", "replace"))
        lines += [
            "",
            f"## Recipe: {name} (.factory/crew/recipes/{name}.md, sha256 {_sha(data)})",
            "",
            (
                "A repeatable kind of mission the user saved. Its defaults are the answers they gave last time:"
                " suggestions to confirm in round 0, never answers."
            ),
            "",
            _demote(body),
        ]
    return "\n".join(lines).rstrip("\n") + "\n"


def snapshot(root, mission_id: str, config: dict, recipe: str | None = None) -> tuple[dict, dict]:
    """What a new or refreshed mission freezes: {relative path: bytes} to write, and mission["crew"]."""
    from .workflow import refuse_secrets

    project = _read(safe_path(root, PROJECT))
    recipe_data = load_recipe(root, recipe) if recipe else None
    context = render_context(mission_id, project, (recipe, recipe_data) if recipe else None).encode()
    files = {f".factory/missions/{mission_id}/{CONTEXT_DOC}": context}
    record = {
        "project_sha256": _sha(project),
        "context_sha256": _sha(context),
        "personal": "disabled",
        "personal_sha256": None,
        "recipe": {"name": recipe, "sha256": _sha(recipe_data)} if recipe else None,
    }
    if settings(config)["personal"]:
        try:
            me = _read(personal_path())
        except OSError:
            me = None
        if me is None:
            record["personal"] = "absent"
        else:
            record["personal_sha256"] = _sha(me)
            try:
                refuse_secrets(me.decode("utf-8", "replace"), "personal profile")
                if len(me) > LIMITS["personal"]:
                    raise FactoryError("personal profile is over its size limit")
                record["personal"] = "present"
                files[personal_snapshot_path(mission_id)] = me
            except FactoryError:
                record["personal"] = "withheld"
    return files, record


def write_snapshot(root, files: dict) -> None:
    for relative, data in files.items():
        if relative.startswith(PRIVATE + "/"):
            _private(root).parent.mkdir(parents=True, exist_ok=True)
            write_bytes(root, relative, data)
        else:
            write_bytes(root, relative, data, mode=0o644)


def project_sections(context_text: str, names: tuple[str, ...]) -> list[tuple[str, str]]:
    """(name, body) of the named project.md sections inside a frozen crew-context.md."""
    found, current, body = [], None, []
    for line in context_text.splitlines():
        if line.startswith("### "):
            if current in names and "".join(body).strip():
                found.append((current, "\n".join(body).strip()))
            current, body = line[4:].strip(), []
        elif line.startswith("## "):
            if current in names and "".join(body).strip():
                found.append((current, "\n".join(body).strip()))
            current, body = None, []
        elif current is not None:
            body.append(line)
    if current in names and "".join(body).strip():
        found.append((current, "\n".join(body).strip()))
    return sorted(found, key=lambda item: names.index(item[0]))


KEEP = object()


def assert_recipe_allowed(source: str | None) -> None:
    if source in ("contributor", "anonymous"):
        raise FactoryError(
            f"A request from a {source} is untrusted input, so it never receives remembered answers (a recipe)"
        )


def set_recipe(root, mission_id: str, name: str | None) -> dict:
    """Choose (or clear) the recipe of a PROPOSED mission; its knowledge is frozen again."""
    if name is not None and not RECIPE_NAME.fullmatch(name):
        raise FactoryError("Recipe name must be lowercase letters, digits and hyphens")
    return refresh(root, mission_id, recipe=name)


def refresh(root, mission_id: str, recipe=KEEP) -> dict:
    """Re-freeze a PROPOSED mission's knowledge after the user saved new knowledge.

    If the only commits since the mission's base change .factory/crew/**, the base advances to
    HEAD (recorded in base_history, naming the ledger entry), so the knowledge commit is not part
    of the mission's candidate.
    """
    from .core import git, load_config
    from .evidence import is_metadata
    from .workflow import load_mission, update_mission

    config = load_config(root)
    current = load_mission(root, mission_id)
    if recipe is KEEP:
        recipe = ((current.get("crew") or {}).get("recipe") or {}).get("name")
    elif recipe is not None:
        assert_recipe_allowed((current.get("request") or {}).get("source"))
    files, record = snapshot(root, mission_id, config, recipe)
    head = git(root, "rev-parse", "HEAD")
    entries = read_ledger(root)

    def mutate(mission):
        if mission["state"] != "PROPOSED" or mission["tasks"]:
            raise FactoryError("crew refresh applies only to a PROPOSED mission without tasks")
        if "crew" not in mission:
            raise FactoryError("This mission was created before saved knowledge; it has nothing to refresh")
        base = mission["base_commit"]
        if base != head:
            changed = git(
                root, "diff", "--no-ext-diff", "--name-only", "--no-renames", base, head, "--"
            ).split()
            outside = [p for p in changed if not p.startswith(CREW_DIR + "/") and not is_metadata(p)]
            try:
                git(root, "merge-base", "--is-ancestor", base, head)
                ancestor = True
            except FactoryError:
                ancestor = False
            if outside or not ancestor:
                raise FactoryError(
                    "The commits since this mission's base change more than .factory/crew "
                    f"({', '.join(outside[:5]) or 'not a descendant'}); create a new mission from the new trunk"
                )
            if changed:
                if not entries:
                    raise FactoryError(
                        "No ledger entry records the knowledge commit; save knowledge with crew apply"
                    )
                mission.setdefault("base_history", []).append(
                    {"from": base, "to": head, "decision": f"CREW-LEDGER-{entries[-1]['seq']}", "at": now()}
                )
                mission["base_commit"] = head
        mission["crew"] = record
        write_snapshot(root, files)
        return mission

    mission = update_mission(root, mission_id, mutate)
    return {"mission": mission_id, "crew": mission["crew"], "base_commit": mission["base_commit"]}
