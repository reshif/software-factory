"""Setup the agent proposes and the user approves: checks, setup commands, limits, ignore rules.

factory.json defines the checks an agent's own work is judged by, so an agent never edits it
(*Authority*; *Never* weaken checks to obtain a pass). That does not mean the user must type
the change. An agent stores an inert proposal (``setup propose``) and shows the exact diff; the
user approves it with their own chat line ``approve S-0001 setup`` or, in a terminal, ``setup
apply`` with the ID typed back. The factory then writes factory.json and the ignore rules,
re-renders the exports, commits exactly those files and moves open missions onto that commit.

What a proposal may change: ``checks``, ``setup``, the two check limits
(``check_timeout_seconds``, ``check_output_bytes``), added ``.gitignore`` lines,
``model_selection`` (which model each role uses) and turning orchestrator enforcement on. Owners,
switching enforcement off, approvals, delivery, JEV and everything else stay the user's own edits. Missions
past PROPOSED keep their scope, but their verification and reviews bind the old configuration
through the candidate fingerprint, so they are stale and run again under the approved setup.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import sys

from .core import FactoryError, git, load_config, now, read_json, safe_path, validate, write_bytes

DIRECTORY = ".factory/local/setup"
PROPOSAL_ID = re.compile(r"S-[0-9]{4,}")
FIELDS = {"reason", "checks", "setup", "limits", "gitignore", "model_selection", "enforcement"}
LIMITS = ("check_timeout_seconds", "check_output_bytes")
IGNORE_MARKER = "# Added by software-factory setup proposals"
MAX_IGNORE_LINES = 400


def _canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _file(root, proposal_id: str):
    if not isinstance(proposal_id, str) or not PROPOSAL_ID.fullmatch(proposal_id):
        raise FactoryError(f"Invalid setup proposal ID: {proposal_id!r} (expected S-0001)")
    return safe_path(root, f"{DIRECTORY}/{proposal_id}.json")


def _hash(proposal: dict) -> str:
    body = {k: proposal[k] for k in ("id", "reason", "factory", "base_sha256", "gitignore")}
    return hashlib.sha256(_canonical(body).encode()).hexdigest()


def load(root, proposal_id: str) -> dict:
    path = _file(root, proposal_id)
    if not path.is_file():
        raise FactoryError(f"No setup proposal {proposal_id}")
    proposal = json.loads(path.read_text(encoding="utf-8"))
    if _hash(proposal) != proposal.get("sha256"):
        raise FactoryError(
            f"Setup proposal {proposal_id} was changed after it was proposed; propose it again"
        )
    return proposal


def proposals(root) -> list[dict]:
    directory = safe_path(root, DIRECTORY)
    found = []
    for path in sorted(directory.glob("S-*.json")) if directory.is_dir() else []:
        try:
            proposal = load(root, path.stem)
            found.append({k: proposal.get(k) for k in ("id", "reason", "status", "created_at", "sha256")})
        except (FactoryError, ValueError, OSError) as exc:
            found.append({"id": path.stem, "status": "invalid", "error": str(exc)})
    return found


SENTINELS = (
    "factory.json", "factory.lock.json", ".gitignore", "AGENTS.md", "CLAUDE.md", "README.md", "pyproject.toml",
    ".factory/CONSTITUTION.md", ".factory/policy.json", ".factory/installation.json", ".factory/src/x.py",
    ".factory/missions/M-1/mission.json", ".factory/missions/M-1/events.jsonl", ".factory/crew/project.md",
    ".factory/crew/ledger.jsonl", ".claude/settings.json", ".claude/skills/factory-build/SKILL.md",
    ".claude/agents/factory-planner.md", ".codex/config.toml", ".agents/skills/factory-build/SKILL.md",
    ".github/copilot-instructions.md", ".github/agents/factory.agent.md", ".github/workflows/ci.yml",
    "src/main.py", "src/pkg/module.py", "app/models.py", "lib/x.js", "tests/test_x.py", "tests/unit/test_y.py",
    "docs/index.md", "Makefile", "tasks.py", "package.json",
)  # fmt: skip


def _ignore_lines(root, lines) -> list[str]:
    """Ignore rules a proposal may add: refused when Git would then ignore factory files, exports,
    test configuration or ordinary source and test paths (probed with git check-ignore)."""
    import os
    import tempfile

    from .workflow import TEST_CONFIG_FILES

    if not isinstance(lines, list) or len(lines) > MAX_IGNORE_LINES:
        raise FactoryError(f"gitignore is a list of at most {MAX_IGNORE_LINES} lines")
    cleaned = []
    for line in lines:
        if not isinstance(line, str) or "\n" in line or len(line) > 200:
            raise FactoryError("Each gitignore line is one line of at most 200 characters")
        text = line.strip()
        if not text or text.startswith("#"):
            continue
        if text.startswith("!"):
            raise FactoryError(
                f"gitignore line {text!r} un-ignores files; a setup proposal only adds ignore rules"
            )
        cleaned.append(text)
    if not cleaned:
        return []
    try:
        lock = read_json(root, "factory.lock.json")
        exports = list((lock.get("generated") or {}).keys())
    except FactoryError:
        exports = []
    probes = sorted(
        {*SENTINELS, *exports, *TEST_CONFIG_FILES, *(f"tests/{name}" for name in TEST_CONFIG_FILES)}
    )

    def ignored(excludes):
        output = git(
            root,
            "-c",
            f"core.excludesFile={excludes}",
            "check-ignore",
            "--no-index",
            "--",
            *probes,
            check=False,
        )
        return set(output.splitlines())

    descriptor, path = tempfile.mkstemp(prefix="sf-ignore-", suffix=".txt")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write("\n".join(cleaned) + "\n")
        hidden = sorted(ignored(path) - ignored(os.devnull))
    finally:
        os.unlink(path)
    if hidden:
        raise FactoryError(
            "These ignore rules would hide files Git must keep seeing (factory files, exports, test "
            "configuration, source or tests): " + ", ".join(hidden[:8]) + "; narrow the rules"
        )
    return cleaned


def _current_ignores(root) -> list[str]:
    path = safe_path(root, ".gitignore")
    return path.read_text(encoding="utf-8").splitlines() if path.is_file() else []


def _merged(root, value: dict) -> dict:
    from .onboarding import PLACEHOLDER_CHECK

    config = read_json(root, "factory.json")
    merged = dict(config)
    for key in ("checks", "setup"):
        if key in value:
            if not isinstance(value[key], list):
                raise FactoryError(f"{key} is a list")
            merged[key] = value[key]
    if "model_selection" in value:
        merged["model_selection"] = value["model_selection"]
    if "enforcement" in value:
        # A proposal can turn enforcement on, never off: switching it off stays the user's own edit.
        if value["enforcement"] != {"claude_orchestrator_agent": True}:
            raise FactoryError(
                'A setup proposal can only turn enforcement on: {"claude_orchestrator_agent": true}'
            )
        merged["enforcement"] = {**(config.get("enforcement") or {}), "claude_orchestrator_agent": True}
    if "limits" in value:
        if not isinstance(value["limits"], dict) or set(value["limits"]) - set(LIMITS):
            raise FactoryError("A setup proposal may change only limits " + " and ".join(LIMITS))
        merged["limits"] = {**config.get("limits", {}), **value["limits"]}
    validate(root, "factory", merged)
    checks = merged.get("checks") or []
    if "checks" not in value:
        return merged
    if any(c.get("id") == PLACEHOLDER_CHECK for c in checks):
        raise FactoryError("Replace the configure-me placeholder: propose the project's real checks")
    if not any(c.get("required") for c in checks):
        raise FactoryError("At least one check must be required, or nothing verifies the work")
    return merged


def _text(config: dict) -> str:
    return json.dumps(config, indent=2, ensure_ascii=False) + "\n"


def propose(root, value: dict) -> dict:
    """Store an inert setup proposal and return the exact change for the user to read."""
    from .calibration import assert_private_directory

    if not isinstance(value, dict) or set(value) - FIELDS:
        raise FactoryError("A setup proposal has only: " + ", ".join(sorted(FIELDS)))
    reason = value.get("reason")
    if not isinstance(reason, str) or not 8 <= len(reason.strip()) <= 300 or "\n" in reason:
        raise FactoryError("reason is one line (8 to 300 characters) saying why the setup changes")
    if not set(value) & {"checks", "setup", "limits", "gitignore", "model_selection", "enforcement"}:
        raise FactoryError(
            "A setup proposal changes at least one of checks, setup, limits, gitignore, model_selection or enforcement"
        )
    current = safe_path(root, "factory.json").read_bytes()
    new_text = _text(_merged(root, value))
    existing = set(_current_ignores(root))
    ignores = [line for line in _ignore_lines(root, value.get("gitignore", [])) if line not in existing]
    if new_text == current.decode("utf-8") and not ignores:
        raise FactoryError("This proposal changes nothing")
    assert_private_directory(root, DIRECTORY)
    directory = safe_path(root, DIRECTORY)
    directory.mkdir(parents=True, exist_ok=True)
    numbers = [int(p.stem[2:]) for p in directory.glob("S-*.json") if PROPOSAL_ID.fullmatch(p.stem)]
    proposal = {
        "id": f"S-{max([0, *numbers]) + 1:04d}",
        "reason": reason.strip(),
        "factory": new_text,
        "base_sha256": hashlib.sha256(current).hexdigest(),
        "gitignore": ignores,
        "status": "proposed",
        "created_at": now(),
    }
    proposal["sha256"] = _hash(proposal)
    with open(_file(root, proposal["id"]), "x", encoding="utf-8") as handle:
        handle.write(json.dumps(proposal, indent=2, ensure_ascii=False) + "\n")
    return {**show(root, proposal["id"]), "approve": _how(proposal["id"])}


def _how(proposal_id: str) -> str:
    return (
        f"Show the user this exact change. To apply it they reply in chat `approve {proposal_id} setup`, or run "
        f"`software-factory setup apply --proposal {proposal_id}` in their terminal. Applying writes the files, "
        "re-renders, commits exactly those files and moves open missions onto that commit."
    )


def show(root, proposal_id: str) -> dict:
    proposal = load(root, proposal_id)
    current = safe_path(root, "factory.json").read_text(encoding="utf-8")
    diff = "".join(
        difflib.unified_diff(
            current.splitlines(keepends=True),
            proposal["factory"].splitlines(keepends=True),
            "factory.json (now)",
            f"factory.json ({proposal_id})",
        )
    )
    new = json.loads(proposal["factory"])
    warnings = _mission_warnings(root)
    return {
        **({"warnings": warnings} if warnings else {}),
        "proposal": proposal_id,
        "status": proposal["status"],
        "reason": proposal["reason"],
        "sha256": proposal["sha256"],
        "checks": [
            {"id": c["id"], "command": c["command"], "required": c.get("required", False)}
            for c in new.get("checks", [])
        ],
        "setup": [{"id": c.get("id"), "command": c.get("command")} for c in new.get("setup", [])],
        "factory_json_diff": diff or "(factory.json unchanged)",
        "gitignore_added": proposal["gitignore"],
    }


def _mission_warnings(root) -> list[str]:
    """Open missions created on a different committed factory.json: they cannot move onto the setup commit."""
    from .workflow import PRE_MERGE_STATES, effective_state, list_missions, load_mission

    warnings = []
    try:
        head = git(root, "rev-parse", "HEAD:factory.json")
    except FactoryError:
        return warnings
    for item in list_missions(root)["missions"]:
        if "error" in item:
            continue
        mission = load_mission(root, item["id"])
        if effective_state(mission) not in PRE_MERGE_STATES:
            continue
        try:
            base = git(root, "rev-parse", f"{mission['base_commit']}:factory.json")
        except FactoryError:
            base = None
        if base != head:
            warnings.append(
                f"factory.json was already changed by a commit after {mission['id']} was created; that mission "
                "will not move onto this proposal's commit and its gate keeps reporting the earlier change"
            )
    return warnings


def _confirm_on_terminal(proposal: dict, view: dict) -> None:
    if not (sys.stdin.isatty() and sys.stderr.isatty()):
        raise FactoryError(
            "setup apply changes the checks the work is judged by, so only the user runs it, in an interactive "
            f"terminal (in Claude Code the user can reply `approve {proposal['id']} setup` instead)"
        )
    sys.stderr.write(
        f"Setup proposal {proposal['id']}: {proposal['reason']}\n\n{view['factory_json_diff']}\n"
        + ("".join(f"+ .gitignore: {line}\n" for line in proposal["gitignore"]))
        + f"\nType the proposal ID ({proposal['id']}) to apply and commit exactly this: "
    )
    sys.stderr.flush()
    if sys.stdin.readline().strip() != proposal["id"]:
        raise FactoryError("Setup not applied: the typed proposal ID did not match")


def commit_paths(root, paths: list[str], message: str) -> str | None:
    """Commit exactly ``paths`` (nothing else staged or unstaged); None when there is nothing to commit."""
    paths = sorted(set(paths))
    git(root, "add", "-A", "--", *paths)
    if not git(root, "diff", "--cached", "--name-only", "--", *paths):
        return None
    git(root, "-c", "user.useConfigOnly=true", "commit", "-q", "-m", message, "--", *paths)
    return git(root, "rev-parse", "HEAD")


def move_missions(root, decision: str, commit: str) -> list[dict]:
    """Move every open pre-merge product mission in this working tree onto the new commit."""
    from .workflow import PRE_MERGE_STATES, effective_state, list_missions, load_mission, rebase_mission

    moved = []
    for item in list_missions(root)["missions"]:
        if "error" in item:
            continue
        mission = load_mission(root, item["id"])
        if effective_state(mission) not in PRE_MERGE_STATES:
            continue
        try:
            result = rebase_mission(
                root, mission["id"], decision=decision, in_flight=True, only_commit=commit
            )
            moved.append(
                {"mission": mission["id"], "state": mission["state"], "base_commit": result["base_commit"]}
            )
        except FactoryError as exc:
            moved.append({"mission": mission["id"], "state": mission["state"], "not_moved": str(exc)})
    return moved


def apply(root, proposal_id: str, *, via: str = "terminal", reference: str | None = None, pin: str | None = None,
          confirm=None) -> dict:  # fmt: skip
    """Apply an approved setup proposal: write, render, commit and move open missions."""
    from .rendering import render
    from .workflow import ACTIVE_TASK_STATES, list_missions, load_mission, setup_problems, state_lock

    proposal = load(root, proposal_id)
    if proposal["status"] != "proposed":
        raise FactoryError(f"Setup proposal {proposal_id} is {proposal['status']}; propose again")
    if pin is not None and not re.fullmatch(r"[0-9a-fA-F]{8,64}", pin):
        raise FactoryError("A pinned hash is the first 8 to 64 hex characters of the proposal's sha256")
    if pin and not proposal["sha256"].startswith(pin.lower()):
        raise FactoryError(
            f"Setup proposal {proposal_id} is not the one approved (hash {proposal['sha256'][:8]})"
        )
    factory = safe_path(root, "factory.json")
    before = factory.read_bytes()
    if hashlib.sha256(before).hexdigest() != proposal["base_sha256"]:
        raise FactoryError(f"factory.json changed since {proposal_id} was proposed; propose it again")
    pending = setup_problems(root)
    if pending:
        raise FactoryError(
            "Other factory setup is unfinished, and applying would mix it into this commit: "
            + "; ".join(pending)
        )
    for item in list_missions(root)["missions"]:
        if "error" not in item and any(
            t["status"] in ACTIVE_TASK_STATES for t in load_mission(root, item["id"])["tasks"]
        ):
            raise FactoryError(f"Stop the active tasks of {item['id']} before its setup changes")
    view = show(root, proposal_id)
    (confirm or (lambda p: _confirm_on_terminal(p, view)))(proposal)
    ignore = safe_path(root, ".gitignore")
    ignore_before = ignore.read_bytes() if ignore.is_file() else None
    who = reference or ("typed confirmation in the user's terminal" if via == "terminal" else via)
    message = (
        f"Factory setup: {proposal['reason']} ({proposal_id})\n\nApproved by the user: {who}\n"
        f"Proposal sha256: {proposal['sha256']}"
    )
    paths = ["factory.json"]
    with state_lock(root):
        try:
            write_bytes(root, "factory.json", proposal["factory"].encode(), mode=0o644)
            if proposal["gitignore"]:
                lines = _current_ignores(root)
                if IGNORE_MARKER not in lines:
                    lines += ["", IGNORE_MARKER]
                write_bytes(
                    root,
                    ".gitignore",
                    ("\n".join([*lines, *proposal["gitignore"]]) + "\n").encode(),
                    mode=0o644,
                )
                paths.append(".gitignore")
            load_config(root)
            rendered = render(root)
            paths += [p for p in rendered.get("changed", []) if isinstance(p, str)]
            commit = commit_paths(root, paths, message)
        except BaseException as exc:
            # Nothing is left half-applied: unstage, restore both files and the exports.
            git(root, "reset", "-q", "--", *sorted(set(paths)), check=False)
            write_bytes(root, "factory.json", before, mode=0o644)
            if ignore_before is not None:
                write_bytes(root, ".gitignore", ignore_before, mode=0o644)
            elif ignore.is_file():
                ignore.unlink()
            try:
                render(root)
            except FactoryError:
                pass
            if isinstance(exc, FactoryError):
                raise FactoryError(f"Setup not applied (everything was restored): {exc}") from exc
            raise
    write_bytes(
        root,
        f"{DIRECTORY}/{proposal_id}.json",
        (json.dumps({**proposal, "status": "applied", "applied_at": now(), "via": via, "commit": commit}, indent=2)
         + "\n").encode(),
    )  # fmt: skip
    return {
        "applied": proposal_id,
        "commit": commit,
        "files": sorted(set(paths)),
        "checks": [c["id"] for c in json.loads(proposal["factory"]).get("checks", [])],
        "missions": move_missions(root, f"SETUP-{proposal_id}", commit) if commit else [],
        "note": "Checks are not run yet; verification runs them. Missions past PROPOSED must verify and be reviewed "
        "again under this setup.",
    }


def handle(args) -> dict:
    root, command = args.root, args.setup_command
    if command == "propose":
        from .workflow import read_text_input

        try:
            value = json.loads(read_text_input(root, args.input, "Setup --input")[1])
        except ValueError as exc:
            raise FactoryError(f"Setup proposal input is not valid JSON: {exc}") from exc
        return propose(root, value)
    if command == "show":
        return show(root, args.proposal)
    if command == "proposals":
        return {"proposals": proposals(root)}
    if command == "apply":
        return apply(root, args.proposal, pin=args.hash)
    raise FactoryError(f"Unknown setup command: {command}")


def add_parser(subparsers) -> None:
    setup = subparsers.add_parser("setup", help="Checks and ignore rules the agent proposes and you approve")
    actions = setup.add_subparsers(dest="setup_command", required=True, metavar="<action>")
    p = actions.add_parser("propose", help="Store an inert setup proposal (checks, setup, limits, gitignore)")
    p.add_argument("--input", required=True, help="Repository-relative JSON file, or - for stdin")
    p = actions.add_parser("show", help="Show a proposal's exact change to factory.json and .gitignore")
    p.add_argument("--proposal", required=True, metavar="S-0001", help="Proposal ID")
    actions.add_parser("proposals", help="List setup proposals")
    p = actions.add_parser("apply", help="Apply and commit a proposal you approved (you, in a terminal)")
    p.add_argument("--proposal", required=True, metavar="S-0001", help="Proposal ID")
    p.add_argument(
        "--hash", help="Optional: the first 8+ hex of the proposal's sha256, to pin what you approved"
    )
    for parser in actions.choices.values():
        parser.set_defaults(handler=handle)
