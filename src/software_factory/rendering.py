"""Native client exports with ownership limited to factory content."""

from __future__ import annotations

import json
import re
from pathlib import Path

import tomlkit
import yaml

from . import __version__
from .core import FactoryError, asset_path, asset_root, load_config, profiles, read_json, safe_path, sha256
from .transactions import apply, ensure_no_journal, optional, planned_preimages, planning_snapshot

START = "<!-- software-factory:start -->"
END = "<!-- software-factory:end -->"
SHARED = {"AGENTS.md", "CLAUDE.md", ".github/copilot-instructions.md"}
ORCHESTRATOR_AGENT = ".claude/agents/factory-orchestrator.md"
GUARD = ".factory/hooks/orchestrator_guard.py"
# Chat approvals (Claude Code): a UserPromptSubmit hook in the project's .claude/settings.json.
CLAUDE_SETTINGS = ".claude/settings.json"
CHAT_APPROVAL = ".factory/hooks/chat_approval.py"
SPECIALISTS = ("factory-planner", "factory-implementer", "factory-verifier", "factory-reviewer")
ENFORCEMENT_FLAGS = {"claude": "claude_orchestrator_agent"}
# Entry prompts named in the rendered CLAUDE.md and copilot-instructions.md text.
ENTRY_PROMPTS = (
    "factory-build", "factory-blueprint", "factory-resume", "factory-status", "factory-onboard", "factory-retro",
)  # fmt: skip
VENV_CLI = ".factory/.venv/bin/software-factory"
CODEX_INSTRUCTIONS_LIMIT = 32768  # Codex project_doc_max_bytes default.
GUARD_SELF_TEST = (
    "First run Bash `true`. The guard must deny it. If it runs, stop and report that enforcement is "
    "inactive (untrusted folder, -p session or hooks disabled)."
)
CRLF_NOTE = "line-ending conversion (CRLF, for example core.autocrlf) also counts as a change"


def guard_command(project: str) -> str:
    """POSIX shell command running the stdlib guard; any launch failure exits 2 (deny).

    `project` is `${CLAUDE_PROJECT_DIR}`, expanded by the shell inside double quotes: the
    expanded value is not re-expanded, word-split or globbed, so a project path containing
    `$`, `"`, backticks or spaces stays one literal path.

    Prefers the pinned runtime interpreter and falls back to python3 (3.11+) when the
    runtime is not hydrated. Claude Code treats exit 2 from a PreToolUse hook as a deny.

    Fail closed only when run: the command needs a POSIX shell (on Windows, Claude Code's Git
    Bash). If the shell itself cannot start it, or the hook times out, the exit is not 2 and
    Claude Code does not block the call; claude.json's limits say so.
    """
    return (
        f'P="{project}/.factory/.venv/bin/python"; [ -x "$P" ] || P=python3; '
        f'"$P" -I -B "{project}/{GUARD}" || exit 2'
    )


def chat_approval_command(project: str) -> str:
    """POSIX shell command running the chat-approval hook on the pinned runtime.

    It records the user's own `approve <MISSION> <kind>` message through the factory code, so it
    needs the hydrated runtime; it never blocks a prompt (a missing runtime or any failure exits 0).
    """
    return (
        f'P="{project}/.factory/.venv/bin/python"; '
        f'[ -x "$P" ] && "$P" -I -B "{project}/{CHAT_APPROVAL}" "{project}" || true'
    )


def chat_approval_entry() -> dict:
    return {"hooks": [{"type": "command", "command": chat_approval_command("${CLAUDE_PROJECT_DIR}")}]}


def chat_approvals_enabled(config: dict, selected_profiles) -> bool:
    return "claude" in selected_profiles and (config.get("approvals") or {}).get("chat", True) is not False


def _entry_sha(entry) -> str:
    return sha256(json.dumps(entry, sort_keys=True, separators=(",", ":")))


def enforcement_settings(config: dict) -> dict[str, bool]:
    settings = config.get("enforcement") or {}
    return {flag: settings.get(flag) is True for flag in ENFORCEMENT_FLAGS.values()}


def _exported(root: Path, name: str) -> bool:
    """Whether the factory currently owns and has written the export `name`."""
    try:
        manifest = read_manifest(root)
        return bool(manifest and name in manifest["generated"] and optional(root, name) is not None)
    except FactoryError:
        return False


def enforcement_summary(root: Path, config: dict) -> dict:
    """Per-profile orchestrator enforcement layer from vendor capabilities; reads only.

    `enabled` means the enforcing export is actually rendered: for Claude the opt-in flag is set
    and the orchestrator agent file is owned and present; Copilot's tool allowlist is always on
    (the factory agent is always exported); Codex has instructions only.
    """
    settings = enforcement_settings(config)
    summary = {}
    for profile in profiles(config):
        vendor = json.loads(asset_path(root, f"vendors/{profile}.json").read_text())
        capabilities = vendor.get("capabilities", {})
        flag = ENFORCEMENT_FLAGS.get(profile)
        layer = capabilities.get("orchestrator_enforcement", "instructions")
        if layer == "hook":
            enabled = bool(flag and settings[flag] and _exported(root, ORCHESTRATOR_AGENT))
        else:
            enabled = layer == "tool_allowlist"
        summary[profile] = {
            "layer": layer if enabled or layer != "hook" else "instructions",
            "opt_in": f"enforcement.{flag}" if flag else None,
            "configured": bool(flag and settings[flag]) if flag else None,
            "enabled": enabled,
            "capabilities": capabilities,
            "scope": {
                "claude": "claude --agent factory-orchestrator in a trusted workspace (not -p sessions)",
                "copilot": "factory agent without the edit tool set; shell writes are instruction-only; "
                "no hooks (preToolUse has no agent identity)",
                "codex": "instructions only",
            }[profile],
            "live_behavior": "not_run",
        }
    return summary


def metadata(text: str) -> tuple[dict, str]:
    match = re.match(r"^---\r?\n(.*?)\r?\n---(?:\r?\n|$)", text, re.DOTALL)
    if not match:
        raise FactoryError("Skill/agent is missing YAML frontmatter")
    try:
        data = yaml.safe_load(match[1])
    except yaml.YAMLError as exc:
        raise FactoryError("Invalid skill/agent YAML") from exc
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("name"), str)
        or not isinstance(data.get("description"), str)
    ):
        raise FactoryError("Skill/agent frontmatter needs name and description")
    return data, text[match.end() :].strip()


def markdown(header: dict, body: str) -> bytes:
    # No line folding: clients that read frontmatter line by line (tools, hook commands) see one line.
    return (
        "---\n"
        + yaml.safe_dump(header, sort_keys=False, width=float("inf")).strip()
        + "\n---\n\n"
        + body.strip()
        + "\n"
    ).encode()


def allowed_export(name: str) -> bool:
    if name in SHARED or name in (".codex/config.toml", CLAUDE_SETTINGS):
        return True
    return (
        bool(
            re.fullmatch(r"\.(claude|agents|github)/skills/factory-[a-z0-9-]+/[A-Za-z0-9_./-]+", name)
            or re.fullmatch(r"\.claude/agents/factory-[a-z0-9-]+\.md", name)
            or re.fullmatch(r"\.codex/agents/factory-[a-z0-9-]+\.toml", name)
            or re.fullmatch(r"\.github/agents/factory(?:-[a-z0-9-]+)?\.agent\.md", name)
        )
        and ".." not in Path(name).parts
    )


TOML_KEYS = ("enabled", "max_concurrent_threads_per_session")  # "enabled": locks before 0.3.2.
RECORD_FIELDS = {
    "file": {"kind": str, "sha256": str},
    "block": {"kind": str, "sha256": str, "separator": str, "existed": bool},
    "toml": {"kind": str, "values": dict, "created_table": bool, "existed": bool, "appended": str},
    "json": {"kind": str, "entry_sha256": str, "created": list, "existed": bool},
}
RECORD_REQUIRED = {
    "file": {"kind", "sha256"},
    "block": {"kind", "sha256"},
    "toml": {"kind", "values"},
    "json": {"kind", "entry_sha256", "created", "existed"},
}
JSON_CONTAINERS = ("hooks", "hooks.UserPromptSubmit")


def _check_record(name: str, record) -> None:
    """Type- and key-check one ownership record; a malformed lock is a FactoryError, never a crash."""
    if not isinstance(record, dict):
        raise FactoryError(f"Invalid renderer ownership: {name}")
    kind = record.get("kind")
    expected_kind = (
        "block"
        if name in SHARED
        else "toml"
        if name == ".codex/config.toml"
        else "json"
        if name == CLAUDE_SETTINGS
        else "file"
    )
    if kind not in RECORD_FIELDS:
        raise FactoryError(f"Invalid renderer ownership: {name}")
    if kind != expected_kind:
        raise FactoryError(f"Invalid ownership kind for {name}")
    fields = RECORD_FIELDS[kind]
    if not RECORD_REQUIRED[kind] <= record.keys() or not record.keys() <= fields.keys():
        raise FactoryError(f"Invalid ownership record for {name}: fields {sorted(record)}")
    for key, value in record.items():
        if type(value) is not fields[key]:
            raise FactoryError(f"Invalid ownership record for {name}: {key}")
    if kind == "json" and (
        not re.fullmatch(r"[0-9a-f]{64}", record["entry_sha256"])
        or any(item not in JSON_CONTAINERS for item in record["created"])
    ):
        raise FactoryError(f"Invalid ownership record for {name}")
    if "sha256" in record and not re.fullmatch(r"[0-9a-f]{64}", record["sha256"]):
        raise FactoryError(f"Invalid ownership record for {name}: sha256")
    if record.get("separator", "") not in ("", "\n", "\n\n"):
        raise FactoryError(f"Invalid ownership record for {name}: separator")
    if kind == "toml":
        for key, value in record["values"].items():
            if key not in TOML_KEYS or type(value) is not (bool if key == "enabled" else int):
                raise FactoryError(f"Invalid TOML ownership key in {name}: {key}")
        if "appended" in record and (len(record["appended"]) > 1024 or not record.get("created_table")):
            raise FactoryError(f"Invalid ownership record for {name}: appended")


def read_manifest(root: Path) -> dict | None:
    if optional(root, "factory.lock.json") is None:
        return None
    data = read_json(root, "factory.lock.json")
    if (
        not isinstance(data, dict)
        or data.get("schema_version") != 2
        or not isinstance(data.get("generated"), dict)
    ):
        raise FactoryError("Unsupported renderer manifest; no changes applied")
    for name, record in data["generated"].items():
        if not allowed_export(name):
            raise FactoryError(f"Invalid renderer ownership: {name}")
        safe_path(root, name)
        _check_record(name, record)
    relinquished = data.get("relinquished", [])
    if not isinstance(relinquished, list):
        raise FactoryError("Invalid relinquished exports in renderer manifest")
    for name in relinquished:
        if (
            not isinstance(name, str)
            or not allowed_export(name)
            or name in SHARED
            or name in data["generated"]
        ):
            raise FactoryError(f"Invalid relinquished export: {name}")
        if name in (".codex/config.toml", CLAUDE_SETTINGS):
            raise FactoryError(f"Invalid relinquished export: {name}")
        safe_path(root, name)
    return data


def _text(data: bytes, name: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FactoryError(
            f"{name} is not valid UTF-8 (byte {exc.start}); save it as UTF-8, then retry"
        ) from None


def _is_whole_file(name: str) -> bool:
    return name not in SHARED and name not in (".codex/config.toml", CLAUDE_SETTINGS)


def _settings_doc(text: str, name: str) -> dict:
    try:
        doc = json.loads(text) if text.strip() else {}
    except ValueError as exc:
        raise FactoryError(f"Invalid Claude settings: {name} is not valid JSON ({exc})") from exc
    if not isinstance(doc, dict):
        raise FactoryError(f"Invalid Claude settings: {name} must be a JSON object")
    hooks = doc.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise FactoryError(f"Invalid Claude settings: `hooks` in {name} must be an object")
    if isinstance(hooks, dict) and not isinstance(hooks.get("UserPromptSubmit", []), list):
        raise FactoryError(f"Invalid Claude settings: `hooks.UserPromptSubmit` in {name} must be a list")
    return doc


def _settings_bytes(doc: dict) -> bytes:
    return (json.dumps(doc, indent=2) + "\n").encode()


def strip_owned(root: Path, name: str, record: dict) -> bytes | None:
    """Return the file without factory-owned content; None means no file remains.

    A missing export is not an error: the user deliberately removed it (or it
    never existed), so there is nothing owned left to strip.
    """
    current = optional(root, name)
    if current is None:
        return None
    if record["kind"] == "file":
        if sha256(current) != record["sha256"]:
            raise FactoryError(
                f"Generated file drift: {name}; restore it (for example git checkout -- {name}) "
                f"or delete it to relinquish factory ownership ({CRLF_NOTE})"
            )
        return None
    text = _text(current, name)
    if record["kind"] == "block":
        if text.count(START) != 1 or text.count(END) != 1:
            raise FactoryError(
                f"Ambiguous or missing owned section: {name}; restore exactly one factory section "
                f"(for example git checkout -- {name})"
            )
        first, last = text.index(START), text.index(END) + len(END)
        if last < first or sha256(text[first:last]) != record["sha256"]:
            raise FactoryError(
                f"Generated section drift: {name}; restore the section between the software-factory "
                f"markers (for example git checkout -- {name}) and keep local edits outside the markers "
                f"({CRLF_NOTE})"
            )
        suffix = text[last:]
        suffix = suffix.removeprefix("\n")
        prefix = text[:first]
        separator = record.get("separator", "")
        if separator and prefix.endswith(separator):
            # The factory added the separator. With user text after the section, one newline
            # still has to end the preceding line; otherwise the whole separator goes.
            prefix = prefix[: -len(separator)] + ("\n" if suffix and separator == "\n\n" else "")
        result = prefix + suffix
        return result.encode() if result or record.get("existed") else None
    if record["kind"] == "json":
        doc = _settings_doc(text, name)
        hooks = doc.get("hooks") or {}
        entries = hooks.get("UserPromptSubmit", [])
        kept = [e for e in entries if _entry_sha(e) != record["entry_sha256"]]
        if len(kept) != len(entries):
            hooks["UserPromptSubmit"] = kept
        if "hooks.UserPromptSubmit" in record["created"] and "UserPromptSubmit" in hooks and not kept:
            del hooks["UserPromptSubmit"]
        if "hooks" in record["created"] and "hooks" in doc and not doc["hooks"]:
            del doc["hooks"]
        if not doc and not record["existed"]:
            return None
        if len(kept) == len(entries) and doc == _settings_doc(text, name):
            return current  # Nothing owned was present: keep the user's bytes exactly.
        return _settings_bytes(doc)
    appended = record.get("appended")
    try:
        doc = tomlkit.parse(text)
        agents = doc.get("agents")
        values = record.get("values", {})
        for key, expected in values.items():
            if key not in TOML_KEYS:
                raise FactoryError("Invalid TOML ownership key")
            found = agents.get(key) if isinstance(agents, dict) else None
            found = found.unwrap() if hasattr(found, "unwrap") else found
            if found != expected or type(found) is not type(expected):
                raise FactoryError(
                    f"Generated TOML key drift: agents.{key} in {name}; restore agents.{key} = "
                    f"{json.dumps(expected)} or restore the file from Git"
                )
        if appended and text.endswith(appended):
            # The factory appended the whole [agents] table: drop exactly those bytes.
            result = text[: -len(appended)]
        else:
            for key in values:
                del agents[key]
            removed_table = record.get("created_table") and "agents" in doc and not doc["agents"]
            if removed_table:
                del doc["agents"]
            result = tomlkit.dumps(doc)
            if removed_table and result.endswith("\n\n"):
                result = result.rstrip("\n") + "\n"  # The blank line tomlkit put before [agents].
        return result.encode() if result.strip() or record.get("existed") else None
    except (ValueError, TypeError) as exc:
        raise FactoryError(f"Cannot reconcile {name}: {exc}") from exc


def _marker_span(text: str, marker: str) -> tuple[int, int]:
    """Span of the marker's whole line (with its terminator), or of the marker alone.

    The line is removed only when it holds nothing but the marker (and
    whitespace); otherwise only the marker itself goes, so no user byte is lost.
    """
    at = text.index(marker)
    start = text.rfind("\n", 0, at) + 1
    newline = text.find("\n", at)
    end = len(text) if newline == -1 else newline + 1
    if text[start:end].strip() == marker:
        return start, end
    return at, at + len(marker)


def unmark_section(root: Path, name: str, record: dict) -> bytes | None:
    """Detach a drifted owned section: drop only its marker lines, keep its edited text.

    Raises FactoryError when the markers are missing, duplicated or out of order.
    """
    current = optional(root, name)
    if current is None or record["kind"] != "block":
        raise FactoryError(f"Not an owned section: {name}")
    text = _text(current, name)
    if text.count(START) != 1 or text.count(END) != 1 or text.index(END) < text.index(START):
        raise FactoryError(f"Ambiguous or missing owned section: {name}")
    (start_from, start_to), (end_from, end_to) = _marker_span(text, START), _marker_span(text, END)
    result = text[:start_from] + text[start_to:end_from] + text[end_to:]
    return result.encode() if result or record.get("existed") else None


def _check_registry(registry, schema: dict) -> None:
    """Validate registry.json: schema shape plus the cross-list rules a schema cannot express."""
    from jsonschema import Draft7Validator

    roles = registry.get("roles") if isinstance(registry, dict) else None
    if isinstance(roles, list) and any(
        isinstance(r, dict) and r.get("name") == "orchestrator" for r in roles
    ):
        raise FactoryError("Invalid registry: role name orchestrator is reserved for the coordinator")
    errors = sorted(Draft7Validator(schema).iter_errors(registry), key=lambda e: str(e.path))
    if errors:
        error = errors[0]
        raise FactoryError(
            f"Invalid registry at {'.'.join(map(str, error.path)) or '<root>'}: {error.message}"
        )
    roles = [role["name"] for role in registry["roles"]]
    names = [f"factory-{name}" for name in roles] + registry["skills"] + registry["entries"]
    duplicates = sorted({name for name in names if names.count(name) > 1})
    if duplicates:
        raise FactoryError(
            "Invalid registry: names must be unique across roles (factory-<role>), skills and entries: "
            + ", ".join(duplicates)
        )
    if sorted(f"factory-{name}" for name in roles) != sorted(SPECIALISTS):
        raise FactoryError(
            "Invalid registry: roles must be exactly the factory specialists "
            f"({', '.join(SPECIALISTS)}) that the orchestrator guard and allowlists name"
        )
    missing = sorted({role["skill"] for role in registry["roles"]} - set(registry["skills"]))
    missing += sorted(set(ENTRY_PROMPTS) - set(registry["entries"]))
    if missing:
        raise FactoryError("Invalid registry: missing required skills or entries: " + ", ".join(missing))


def plan_render(
    root: Path,
    *,
    config: dict | None = None,
    payload: dict[str, bytes] | None = None,
    selected=None,
    restore=False,
) -> tuple[dict, dict]:
    """Plan exports.

    A whole-file export the user deleted is a deliberate removal: init, upgrade
    and checks relinquish it (the ownership record moves to ``relinquished`` in
    factory.lock.json and the file is not recreated). Only an explicit
    ``render`` (``restore=True``) recreates relinquished exports that are absent.
    A relinquished path that exists again is user-owned and is never overwritten,
    and stays relinquished while another profile set is active (so switching back
    does not collide with it). The report's ``warnings`` list skipped skill files
    and size limits; nothing in it blocks the plan.
    """
    config = dict(config or load_config(root))
    selected_profiles = profiles(selected or config)
    config["profile"] = selected_profiles[0] if len(selected_profiles) == 1 else selected_profiles
    source_hashes = {}
    warnings = []

    def source(name: str) -> bytes:
        relative = ".factory/" + name
        data = payload[relative] if payload and relative in payload else asset_path(root, name).read_bytes()
        source_hashes[relative] = sha256(data)
        return data

    def source_text(name: str) -> str:
        return _text(source(name), ".factory/" + name)

    try:
        registry = json.loads(source("registry.json"))
    except ValueError as exc:
        raise FactoryError(f"Invalid registry: .factory/registry.json is not JSON ({exc})") from None
    constitution = source_text("CONSTITUTION.md")
    for name in ("policy.json", "workflow.json"):
        source(name)
    for section in ("schemas", "models", "vendors"):
        base = safe_path(root, f".factory/{section}")
        if not base.is_dir():
            if (root / ".factory/installation.json").is_file() and not payload:
                raise FactoryError(f"Missing installed asset directory: {section}")
            base = asset_root() / section
        if payload:
            names = [p.removeprefix(".factory/") for p in payload if p.startswith(f".factory/{section}/")]
        else:
            names = [section + "/" + p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file()]
        for name in names:
            source(name)
    try:
        registry_schema = json.loads(source("schemas/registry.schema.json"))
    except ValueError as exc:
        raise FactoryError(f"Cannot load registry schema: {exc}") from None
    _check_registry(registry, registry_schema)
    roles = {
        name: source_text(f"roles/{name}.md")
        for name in ("orchestrator", *(r["name"] for r in registry["roles"]))
    }
    enforcement = enforcement_settings(config)
    orchestrator_agent = "claude" in selected_profiles and enforcement["claude_orchestrator_agent"]
    if orchestrator_agent:
        source(GUARD.removeprefix(".factory/"))  # Must exist; its hash pins the rendered hook.
    outputs = {}
    prefix = {"claude": "/", "codex": "$", "copilot": "/"}
    commands = [
        {"profile": p, "invoke": prefix[p] + name, "name": name}
        for p in selected_profiles
        for name in registry["entries"]
    ]
    labels = {"claude": "Claude Code", "codex": "Codex", "copilot": "Copilot"}
    groups: dict[str, list[str]] = {}
    for p in selected_profiles:
        groups.setdefault(prefix[p], []).append(labels[p])
    entry_text = "; ".join(
        "/".join(clients) + ": " + ", ".join(mark + name for name in registry["entries"])
        for mark, clients in groups.items()
    )
    layers = {
        "claude": "PreToolUse guard via `claude --agent factory-orchestrator`"
        if orchestrator_agent
        else "instructions (opt-in: enforcement.claude_orchestrator_agent)",
        "codex": "instructions only",
        "copilot": "factory agent without edit tools; shell writes are instruction-only",
    }
    enforcement_text = "; ".join(f"{labels[p]}: {layers[p]}" for p in selected_profiles)
    codex_command = (
        f" Codex sandboxes cannot write the uv cache: there run `{VENV_CLI} ...` with the same arguments."
        if "codex" in selected_profiles
        else ""
    )
    preamble = (
        f"The constitution in AGENTS.md applies; read .factory/CONSTITUTION.md (SHA-256 {sha256(constitution)}) "
        "only if it is not in your context."
    )
    common = (
        constitution.strip()
        + "\n\n## Factory session entry\n\nThis entry applies to the main session only; a delegated factory specialist follows its agent file and brief instead. Read factory.json and .factory/roles/orchestrator.md for factory tasks. Use the canonical .factory/skills workflows and delegate bounded tasks to the installed factory specialists. One writer per workspace. Existing product instructions and host permissions remain in force.\n\n"
        + f"Python command: `uv run --locked --project .factory software-factory doctor` (the pinned `{VENV_CLI}` is equivalent).{codex_command}\nEntry prompts: "
        + entry_text
        + ". Planning is draft-only; status is read-only. Model selection uses `software-factory models plan`;"
        + " `jev.enabled` in factory.json selects JEV instead of the built-in factory-models selector and"
        + " also enables optional JEV claim assessment (consult `software-factory semantic status` before use).\n"
        + "Orchestrator enforcement (local guardrail, unattested): "
        + enforcement_text
        + ".\n"
    )
    if len(common.encode()) > 30000:
        raise FactoryError("Factory instructions exceed supported size")
    outputs["AGENTS.md"] = common.encode()
    if "claude" in selected_profiles:
        outputs["CLAUDE.md"] = (
            b"@AGENTS.md\n\nUse /factory-build, /factory-blueprint, /factory-resume or /factory-status; /factory-onboard records project knowledge and /factory-retro turns a finished mission into lessons. The constitution in AGENTS.md applies; read .factory/CONSTITUTION.md only if it is not in your context. Follow the assigned canonical skill.\n"
            + (
                b"For hook-enforced orchestration start `claude --agent factory-orchestrator`.\n"
                if orchestrator_agent
                else b""
            )
        )
    if "copilot" in selected_profiles:
        outputs[".github/copilot-instructions.md"] = (
            b"Follow AGENTS.md for factory work. Factory missions are supported in VS Code Local (the local "
            b"agent session in VS Code): select the factory agent and invoke /factory-build, /factory-blueprint, "
            b"/factory-resume, /factory-status, /factory-onboard or /factory-retro. Copilot CLI and the Copilot cloud agent are not supported for "
            b"missions: do not start or advance a mission there.\n"
        )
    copilot_prefix = "factory-copilot-" if "claude" in selected_profiles else "factory-"
    for role in registry["roles"]:
        name = role["name"]
        body = (
            f"{preamble} Your role instructions follow. Assigned skill: {role['skill']}. Work only from the generated brief the orchestrator provides (software-factory mission brief); report missing brief fields instead of guessing.\n\n"
            + roles[name]
        )
        # Among the read roles only the planner researches documentation on the web;
        # the reviewer judges the diff and evidence, not the web.
        web = name == "planner"
        if "claude" in selected_profiles:
            tools = (
                ("Read, Glob, Grep, WebFetch, WebSearch, Skill" if web else "Read, Glob, Grep, Skill")
                if role["capability"] == "read"
                else "Read, Glob, Grep, Bash, Skill"
                if role["capability"] == "verify"
                else "Read, Glob, Grep, Bash, Edit, Write, WebFetch, WebSearch, Skill"
            )
            outputs[f".claude/agents/factory-{name}.md"] = markdown(
                {
                    "name": f"factory-{name}",
                    "description": role["description"],
                    "tools": tools,
                    "skills": [role["skill"]],
                },
                body,
            )
        if "codex" in selected_profiles:
            doc = tomlkit.document()
            doc["name"] = f"factory-{name}"
            doc["description"] = role["description"]
            if role["capability"] == "read":
                doc["sandbox_mode"] = "read-only"
            doc["developer_instructions"] = body
            outputs[f".codex/agents/factory-{name}.toml"] = tomlkit.dumps(doc).encode()
        if "copilot" in selected_profiles:
            tools = (
                (["read", "search", "web"] if web else ["read", "search"])
                if role["capability"] == "read"
                else ["read", "search", "execute"]
                if role["capability"] == "verify"
                else ["read", "search", "edit", "execute"]
            )
            outputs[f".github/agents/{copilot_prefix}{name}.agent.md"] = markdown(
                {
                    "name": copilot_prefix + name,
                    "description": role["description"],
                    "tools": tools,
                    "agents": [],
                    "user-invocable": False,
                    "include-custom-instructions": True,
                },
                body,
            )
    if "copilot" in selected_profiles:
        outputs[".github/agents/factory.agent.md"] = markdown(
            {
                "name": "factory",
                "description": "Coordinate factory missions, bounded specialists and evidence.",
                "tools": ["read", "search", "execute", "agent"],
                "agents": [copilot_prefix + r["name"] for r in registry["roles"]],
            },
            preamble + "\n\n" + roles["orchestrator"],
        )
    if orchestrator_agent:
        outputs[ORCHESTRATOR_AGENT] = markdown(
            {
                "name": "factory-orchestrator",
                "description": "Coordinate factory missions: brief, delegate to factory specialists, record "
                "state, verify and decide. Never edits files.",
                "tools": f"Agent({', '.join(SPECIALISTS)}), Read, Glob, Grep, Bash, AskUserQuestion, TodoWrite",
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": guard_command("${CLAUDE_PROJECT_DIR}"),
                                }
                            ],
                        }
                    ]
                },
            },
            f"{GUARD_SELF_TEST}\n\n{preamble}\n\n"
            "## Enforced session\n\n"
            "Start this agent as the main session with `claude --agent factory-orchestrator` in a trusted "
            "workspace. The `Agent(...)` allowlist applies only there; frontmatter hooks are skipped until the "
            "workspace trust dialog is accepted and in `-p` sessions. A PreToolUse guard "
            f"(`{GUARD}`) denies Edit, Write, MultiEdit, NotebookEdit, Skill, unknown tools, Agent calls "
            f"for any subagent other than {', '.join(SPECIALISTS)}, and shell commands outside its "
            "read-only allowlist (project-scoped software-factory commands with their listed options only, "
            "without init, upgrade, uninstall, recover, render, auth, models discover, mission approve, "
            "mission ci-result, verify --candidate-root "
            f"or --root, through `uv run --locked --project .factory software-factory` or `{VENV_CLI}`; "
            "read-only git; ls/cat/head/tail/wc/grep/rg/find; project-relative arguments only, with no "
            "absolute path, leading ~ or .. component). Pass JSON input on stdin with a quoted heredoc "
            "(`--input - <<'JSON'`) instead of writing files. When a call is denied, delegate the work named "
            "in the reason; never try to bypass the guard. Scope, exception, merge and release approvals and CI "
            "results are the user's to record: they approve by replying `approve <MISSION> <kind>` in chat "
            "(the factory's chat hook records it) or with `mission approve` in their terminal, and record CI "
            "with `mission ci-result`; give them the exact line or command. It is a local guardrail: records remain "
            "local-unattested and live behaviour is not_run.\n\n" + roles["orchestrator"],
        )
    trees = [
        f".{ {'claude': 'claude', 'codex': 'agents', 'copilot': 'github'}[p] }/skills"
        for p in selected_profiles
        if p != "copilot" or len(selected_profiles) == 1
    ]
    # Trees where non-entry skills are hidden from the slash menu. Copilot also reads .agents/skills
    # (Codex ignores frontmatter keys it does not know), so it is included when Copilot is active.
    hidden_trees = {".claude/skills", ".github/skills"} | (
        {".agents/skills"} if "copilot" in selected_profiles else set()
    )
    names = set(registry["skills"] + registry["entries"])
    for name in registry["skills"]:
        if payload is not None:
            if f".factory/skills/{name}/SKILL.md" not in payload:
                raise FactoryError(f"Required skill is missing: {name}/SKILL.md")
            asset_names = [
                p.removeprefix(".factory/") for p in payload if p.startswith(f".factory/skills/{name}/")
            ]
        else:
            base = asset_path(root, f"skills/{name}/SKILL.md").parent
            asset_names = [
                "skills/" + name + "/" + p.relative_to(base).as_posix()
                for p in base.rglob("*")
                if p.is_file()
            ]
        for asset in sorted(asset_names):
            if not allowed_export(".claude/" + asset):
                # E.g. an editor backup (SKILL.md~): never exported, so never recorded in the lock.
                warnings.append(
                    f"Skipped .factory/{asset}: skill file names may use only letters, digits, _ . / -; "
                    "rename or remove it"
                )
                continue
            content = source(asset)
            exported = {tree: content for tree in trees}
            if asset.endswith("/SKILL.md"):
                try:
                    meta, skill_body = metadata(_text(content, ".factory/" + asset))
                except FactoryError as exc:
                    raise FactoryError(f"{exc}: .factory/{asset}") from exc
                if meta["name"] != name:
                    raise FactoryError(f"Skill folder/name mismatch: {name}")
                if asset == f"skills/{name}/SKILL.md":
                    hidden = markdown({**meta, "user-invocable": False}, skill_body)
                    exported.update({tree: hidden for tree in trees if tree in hidden_trees})
            for tree in trees:
                outputs[tree + "/" + asset.removeprefix("skills/")] = exported[tree]
    entry_paths = []
    for name in registry["entries"]:
        header, body = metadata(source_text(f"prompts/{name}.md"))
        for key in ("argument-hint", "default-prompt"):
            if not isinstance(header.get(key), str) or not header[key].strip():
                raise FactoryError(f"Entry prompt .factory/prompts/{name}.md needs a nonempty {key} field")
        for tree in trees:
            fields = {"name": name, "description": header["description"]}
            if tree != ".agents/skills" or "copilot" in selected_profiles:
                fields.update({"argument-hint": header["argument-hint"], "user-invocable": True})
            outputs[f"{tree}/{name}/SKILL.md"] = markdown(fields, body)
            entry_paths.append(f"{tree}/{name}/SKILL.md")
            if tree == ".agents/skills":
                outputs[f"{tree}/{name}/agents/openai.yaml"] = yaml.safe_dump(
                    {"interface": {"default_prompt": header["default-prompt"]}}
                ).encode()
                entry_paths.append(f"{tree}/{name}/agents/openai.yaml")
    # Rendered entry-point skills, kept before relinquished exports are dropped below.
    entry_exports = {name: outputs[name] for name in entry_paths}
    previous = read_manifest(root)
    old = previous["generated"] if previous else {}
    relinquished_before = set(previous.get("relinquished", [])) if previous else set()
    relinquished = set()
    pending = []
    for name in sorted(outputs):
        if not _is_whole_file(name):
            continue
        present = optional(root, name) is not None
        if name in old:
            if not present and not restore:
                # Deleted owned export: deliberate removal, so relinquish it.
                relinquished.add(name)
                pending.append(name)
        elif name in relinquished_before and (present or not restore):
            relinquished.add(name)
    # A relinquished file another profile set does not render stays user-owned while it exists.
    relinquished |= {n for n in relinquished_before - outputs.keys() if optional(root, n) is not None}
    for name in relinquished:
        outputs.pop(name, None)
    for tree in (".claude/skills", ".agents/skills", ".github/skills"):
        base = safe_path(root, tree)
        if base.exists():
            for item in sorted(base.glob("*/SKILL.md")):
                rel = item.relative_to(root).as_posix()
                if item.parent.name in names and rel not in old and rel not in relinquished:
                    raise FactoryError(f"Existing skill name collision: {rel}")
                if item.parent.is_symlink() or item.is_symlink():
                    # A linked personal skill is the user's; its target is not scanned.
                    warnings.append(f"Skipped symlinked user skill {rel} in the skill name collision check")
                    continue
                safe_path(root, rel)
                if rel in old or rel in relinquished:
                    continue
                try:
                    meta, _ = metadata(item.read_text())
                except (FactoryError, UnicodeDecodeError, OSError):
                    continue  # Unrelated user skill; its format is not the factory's concern.
                if meta["name"] in names:
                    raise FactoryError(f"Existing skill name collision: {rel}")
    changes = {}
    generated = {}
    bases = {name: strip_owned(root, name, record) for name, record in old.items()}
    for name in (
        old.keys()
        - outputs.keys()
        - ({".codex/config.toml"} if "codex" in selected_profiles else set())
        - ({CLAUDE_SETTINGS} if chat_approvals_enabled(config, selected_profiles) else set())
    ):
        changes[name] = bases[name]
    for name, content in outputs.items():
        base = bases[name] if name in bases else optional(root, name)
        if name in SHARED:
            text = _text(base or b"", name)
            if START in text or END in text:
                raise FactoryError(
                    f"Unowned factory section: {name}; the factory does not own this section (for example "
                    "it was edited before uninstall). Remove it including the software-factory markers, "
                    "or restore the file from Git"
                )
            separator = "\n\n" if text and not text.endswith("\n") else "\n" if text else ""
            block = START + "\n" + content.decode().strip() + "\n" + END
            owned = name in old and optional(root, name) is not None
            if owned:
                current = _text(optional(root, name), name)
                first, last = current.index(START), current.index(END) + len(END)
                changes[name] = (current[:first] + block + current[last:]).encode()
                separator = old[name].get("separator", "")
            else:
                changes[name] = (text + separator + block + "\n").encode()
            generated[name] = {
                "kind": "block",
                "sha256": sha256(block),
                "separator": separator,
                "existed": old[name].get("existed", False) if owned else base is not None,
            }
        else:
            if base is not None:
                raise FactoryError(
                    f"Unowned file collision: {name}; move or delete the existing file, then retry"
                )
            changes[name] = content
            generated[name] = {"kind": "file", "sha256": sha256(content)}
    if chat_approvals_enabled(config, selected_profiles):
        name = CLAUDE_SETTINGS
        base = bases[name] if name in bases else optional(root, name)
        doc = _settings_doc(_text(base or b"", name), name)
        entry, created = chat_approval_entry(), []
        if "hooks" not in doc:
            doc["hooks"], created = {}, ["hooks"]
        if "UserPromptSubmit" not in doc["hooks"]:
            doc["hooks"]["UserPromptSubmit"] = []
            created.append("hooks.UserPromptSubmit")
        if entry not in doc["hooks"]["UserPromptSubmit"]:
            doc["hooks"]["UserPromptSubmit"].append(entry)
        changes[name] = _settings_bytes(doc)
        generated[name] = {
            "kind": "json",
            "entry_sha256": _entry_sha(entry),
            "created": old[name]["created"] if name in old else created,
            "existed": old[name]["existed"] if name in old else base is not None,
        }
    if "codex" in selected_profiles:
        name = ".codex/config.toml"
        base = bases[name] if name in bases else optional(root, name)
        text = _text(base or b"", name)
        try:
            doc = tomlkit.parse(text)
        except (ValueError, TypeError) as exc:
            raise FactoryError(f"Invalid Codex configuration: {exc}") from exc
        agents, features = doc.get("agents"), doc.get("features")
        if agents is not None and not isinstance(agents, dict):
            raise FactoryError(f"Invalid Codex configuration: `{name}` agents must be a table ([agents])")
        if isinstance(features, dict) and features.get("multi_agent") is False:
            raise FactoryError(
                f"Existing Codex setting conflicts: `{name}` features.multi_agent = false disables the "
                "subagents the factory delegates to; remove it or set it to true, then rerun"
            )
        if isinstance(agents, dict) and "enabled" in agents and agents["enabled"] is not True:
            found = agents["enabled"]
            found = found.unwrap() if hasattr(found, "unwrap") else found
            raise FactoryError(
                f"Existing Codex setting conflicts: `{name}` [agents].enabled is set by you to "
                f"{json.dumps(found, default=str)}; the factory needs true — change or remove it, then rerun"
            )
        # The factory no longer writes agents.enabled (older Codex rejects unknown [agents] keys).
        # Either spelling of the thread limit satisfies it; max_threads is the legacy alias.
        values = {}
        record = {"kind": "toml", "values": values, "created_table": False, "existed": base is not None}
        limits = ("max_concurrent_threads_per_session", "max_threads")
        if not (isinstance(agents, dict) and any(key in agents for key in limits)):
            values["max_concurrent_threads_per_session"] = 3
            if agents is None:
                # Append the table as text so removing it later restores the user's bytes exactly.
                lead = "" if not text else "\n" if text.endswith("\n") else "\n\n"
                appended = lead + "[agents]\nmax_concurrent_threads_per_session = 3\n"
                record.update({"created_table": True, "appended": appended})
                text += appended
            else:
                agents["max_concurrent_threads_per_session"] = 3
                text = tomlkit.dumps(doc)
        try:
            tomlkit.parse(text)
        except (ValueError, TypeError) as exc:
            raise FactoryError(f"Invalid Codex configuration: {exc}") from exc
        changes[name] = text.encode()
        generated[name] = record
        size = len(changes.get("AGENTS.md", b""))
        if size > CODEX_INSTRUCTIONS_LIMIT:
            warnings.append(
                f"AGENTS.md is {size} bytes; Codex reads only the first {CODEX_INSTRUCTIONS_LIMIT} bytes by "
                "default (project_doc_max_bytes), so later instructions may be ignored"
            )
    config_bytes = (json.dumps(config, indent=2) + "\n").encode()
    existing_config = optional(root, "factory.json")
    if existing_config is not None and json.loads(existing_config) == config:
        config_bytes = existing_config
    write_config = selected is not None or existing_config is None
    # The recorded hash is of factory.json as it will be on disk after this plan.
    source_hashes["factory.json"] = sha256(config_bytes if write_config else existing_config)
    manifest = {
        "schema_version": 2,
        "factory_version": __version__,
        "profile": selected_profiles,
        "constitution_hash": sha256(constitution),
        "renderer_hash": sha256(Path(__file__).read_bytes()),
        "sources": dict(sorted(source_hashes.items())),
        "generated": dict(sorted(generated.items())),
        "validation": {"live_discovery": "not_run", "live_behavior": "not_run"},
    }
    if relinquished:
        manifest["relinquished"] = sorted(relinquished)
    changes["factory.lock.json"] = (json.dumps(manifest, indent=2) + "\n").encode()
    if write_config:
        changes["factory.json"] = config_bytes
    return changes, {
        "profiles": selected_profiles,
        "prompt_commands": commands,
        "manifest": manifest,
        "relinquished": sorted(relinquished),
        "pending_relinquish": pending,
        "entry_exports": entry_exports,
        "warnings": warnings,
    }


def render(root: Path, *, check=False, selected=None, dry_run=False) -> dict:
    installation = optional(root, ".factory/installation.json")
    if check and installation is not None:
        try:
            uninstalled = json.loads(installation).get("uninstalled") is True
        except (ValueError, AttributeError):
            uninstalled = False  # Reported by doctor's installation checks.
        if uninstalled:
            raise FactoryError(
                "The factory is uninstalled from this project, so there are no generated exports to check; "
                "run software-factory init to reinstall"
            )
    if not check:
        ensure_no_journal(root)
    snapshot = planning_snapshot(root)
    # Explicit render restores deleted exports; --check reports them as relinquished.
    changes, report = plan_render(root, selected=selected, restore=not check)
    expected = planned_preimages(root, snapshot, changes)
    drift = [name for name, data in changes.items() if optional(root, name) != data]
    if check and report["pending_relinquish"] and "factory.lock.json" in drift:
        # Deleting an export is a deliberate removal, not staleness: accept the
        # lock when it differs only by that not-yet-recorded relinquishment.
        recorded = json.loads(optional(root, "factory.lock.json"))
        for name in report["pending_relinquish"]:
            recorded["generated"].pop(name, None)
        recorded["relinquished"] = sorted(set(recorded.get("relinquished", [])) | set(report["relinquished"]))
        if recorded == report["manifest"]:
            drift.remove("factory.lock.json")
    if check and drift:
        raise FactoryError(
            "Generated exports are stale: "
            + ", ".join(drift)
            + f"; run software-factory render to regenerate them ({CRLF_NOTE})"
        )
    if not check and not dry_run:
        apply(root, changes, expected=expected, label="render")
    # A relinquished entry-point skill is either absent or answers the entry prompt
    # with content the factory does not render.
    entry_skills = {}
    for name in report["relinquished"]:
        if name in report["entry_exports"]:
            current = optional(root, name)
            if current is None:
                entry_skills[name] = "missing"
            elif current != report["entry_exports"][name]:
                entry_skills[name] = "replaced"
    return {
        "ok": True,
        "mode": "check" if check else "dry-run" if dry_run else "render",
        "changed": drift,
        "profiles": report["profiles"],
        "prompt_commands": report["prompt_commands"],
        "relinquished": report["relinquished"],
        "relinquished_entry_skills": entry_skills,
        "warnings": report["warnings"],
        "live_behavior": "not_run",
    }
