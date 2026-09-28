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
SPECIALISTS = ("factory-planner", "factory-implementer", "factory-verifier", "factory-reviewer")
ENFORCEMENT_FLAGS = {"claude": "claude_orchestrator_agent"}


def guard_command(project: str) -> str:
    """POSIX shell command running the stdlib guard; any launch failure exits 2 (deny).

    `project` is `${CLAUDE_PROJECT_DIR}`, expanded by the shell inside double quotes: the
    expanded value is not re-expanded, word-split or globbed, so a project path containing
    `$`, `"`, backticks or spaces stays one literal path.

    Prefers the pinned runtime interpreter and falls back to python3 (3.11+) when the
    runtime is not hydrated. Claude Code treats exit 2 from a PreToolUse hook as a deny.
    """
    return (
        f'P="{project}/.factory/.venv/bin/python"; [ -x "$P" ] || P=python3; '
        f'"$P" -I -B "{project}/{GUARD}" || exit 2'
    )


def enforcement_settings(config: dict) -> dict[str, bool]:
    settings = config.get("enforcement") or {}
    return {flag: settings.get(flag) is True for flag in ENFORCEMENT_FLAGS.values()}


def enforcement_summary(root: Path, config: dict) -> dict:
    """Per-profile orchestrator enforcement layer from vendor capabilities; reads only."""
    settings = enforcement_settings(config)
    summary = {}
    for profile in profiles(config):
        vendor = json.loads(asset_path(root, f"vendors/{profile}.json").read_text())
        capabilities = vendor.get("capabilities", {})
        flag = ENFORCEMENT_FLAGS.get(profile)
        enabled = bool(flag and settings[flag] and capabilities.get("orchestrator_enforcement") == "hook")
        layer = capabilities.get("orchestrator_enforcement", "instructions")
        summary[profile] = {
            "layer": layer if enabled or layer != "hook" else "instructions",
            "opt_in": f"enforcement.{flag}" if flag else None,
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
    if name in SHARED or name == ".codex/config.toml":
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


def read_manifest(root: Path) -> dict | None:
    if optional(root, "factory.lock.json") is None:
        return None
    data = read_json(root, "factory.lock.json")
    if data.get("schema_version") != 2 or not isinstance(data.get("generated"), dict):
        raise FactoryError("Unsupported renderer manifest; no changes applied")
    for name, record in data["generated"].items():
        if not allowed_export(name) or record.get("kind") not in ("file", "block", "toml"):
            raise FactoryError(f"Invalid renderer ownership: {name}")
        safe_path(root, name)
        expected_kind = "block" if name in SHARED else "toml" if name == ".codex/config.toml" else "file"
        if record["kind"] != expected_kind:
            raise FactoryError(f"Invalid ownership kind for {name}")
        if (record["kind"] == "block" and name not in SHARED) or (
            record["kind"] == "toml" and name != ".codex/config.toml"
        ):
            raise FactoryError(f"Invalid shared ownership: {name}")
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
        if name == ".codex/config.toml":
            raise FactoryError(f"Invalid relinquished export: {name}")
        safe_path(root, name)
    return data


def _is_whole_file(name: str) -> bool:
    return name not in SHARED and name != ".codex/config.toml"


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
                "or delete it to relinquish factory ownership"
            )
        return None
    text = current.decode()
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
                f"markers (for example git checkout -- {name}) and keep local edits outside the markers"
            )
        suffix = text[last:]
        suffix = suffix.removeprefix("\n")
        prefix = text[:first]
        separator = record.get("separator", "")
        if separator and not suffix and prefix.endswith(separator):
            prefix = prefix[: -len(separator)]
        result = prefix + suffix
        return result.encode() if result or record.get("existed") else None
    try:
        doc = tomlkit.parse(text)
        values = record.get("values", {})
        for key, expected in values.items():
            if key not in ("enabled", "max_concurrent_threads_per_session"):
                raise FactoryError("Invalid TOML ownership key")
            if doc.get("agents", {}).get(key) != expected:
                raise FactoryError(
                    f"Generated TOML key drift: agents.{key} in {name}; restore agents.{key} = "
                    f"{json.dumps(expected)} or restore the file from Git"
                )
            del doc["agents"][key]
        if record.get("created_table") and "agents" in doc and not doc["agents"]:
            del doc["agents"]
        result = tomlkit.dumps(doc)
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
    text = current.decode()
    if text.count(START) != 1 or text.count(END) != 1 or text.index(END) < text.index(START):
        raise FactoryError(f"Ambiguous or missing owned section: {name}")
    (start_from, start_to), (end_from, end_to) = _marker_span(text, START), _marker_span(text, END)
    result = text[:start_from] + text[start_to:end_from] + text[end_to:]
    return result.encode() if result or record.get("existed") else None


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
    A relinquished path that exists again is user-owned and is never overwritten.
    """
    config = dict(config or load_config(root))
    selected_profiles = profiles(selected or config)
    config["profile"] = selected_profiles[0] if len(selected_profiles) == 1 else selected_profiles
    source_hashes = {}

    def source(name: str) -> bytes:
        relative = ".factory/" + name
        data = payload[relative] if payload and relative in payload else asset_path(root, name).read_bytes()
        source_hashes[relative] = sha256(data)
        return data

    registry = json.loads(source("registry.json"))
    constitution = source("CONSTITUTION.md").decode()
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
            base = root / ".factory" / section if (root / ".factory" / section).is_dir() else base
            names = [section + "/" + p.relative_to(base).as_posix() for p in base.rglob("*") if p.is_file()]
        for name in names:
            source(name)
    roles = {
        name: source(f"roles/{name}.md").decode()
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
    common = (
        constitution.strip()
        + "\n\n## Factory session entry\n\nRead factory.json and .factory/roles/orchestrator.md for factory tasks. Use the canonical .factory/skills workflows and delegate bounded tasks to the installed factory specialists. One writer per workspace. Existing product instructions and host permissions remain in force.\n\nPython command: `uv run --locked --project .factory software-factory doctor`.\nEntry prompts: "
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
            b"@AGENTS.md\n\nUse /factory-build, /factory-blueprint, /factory-resume or /factory-status. The constitution in AGENTS.md applies; read .factory/CONSTITUTION.md only if it is not in your context. Follow the assigned canonical skill.\n"
            + (
                b"For hook-enforced orchestration start `claude --agent factory-orchestrator`.\n"
                if orchestrator_agent
                else b""
            )
        )
    if "copilot" in selected_profiles:
        outputs[".github/copilot-instructions.md"] = (
            b"Follow AGENTS.md for factory work. Select the factory agent and invoke /factory-build, /factory-blueprint, /factory-resume or /factory-status.\n"
        )
    copilot_prefix = "factory-copilot-" if "claude" in selected_profiles else "factory-"
    for role in registry["roles"]:
        name = role["name"]
        body = (
            f"The constitution in AGENTS.md applies; read .factory/CONSTITUTION.md (SHA-256 {sha256(constitution)}) only if it is not in your context. Your role instructions follow. Assigned skill: {role['skill']}. Work only from the generated brief the orchestrator provides (software-factory mission brief); report missing brief fields instead of guessing.\n\n"
            + roles[name]
        )
        if "claude" in selected_profiles:
            tools = (
                "Read, Glob, Grep, WebFetch, WebSearch, Skill"
                if role["capability"] == "read"
                else "Read, Glob, Grep, Bash, Skill"
                if role["capability"] == "verify"
                else "Read, Glob, Grep, Bash, Edit, Write, Skill"
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
                ["read", "search", "web"]
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
                },
                body,
            )
    if "copilot" in selected_profiles:
        outputs[".github/agents/factory.agent.md"] = markdown(
            {
                "name": "factory",
                "description": "Coordinate factory missions, bounded specialists and evidence.",
                "tools": ["read", "search", "web", "execute", "agent"],
                "agents": [copilot_prefix + r["name"] for r in registry["roles"]],
            },
            roles["orchestrator"],
        )
    if orchestrator_agent:
        outputs[ORCHESTRATOR_AGENT] = markdown(
            {
                "name": "factory-orchestrator",
                "description": "Coordinate factory missions: brief, delegate to factory specialists, record "
                "state, verify and decide. Never edits files.",
                "tools": f"Agent({', '.join(SPECIALISTS)}), Read, Glob, Grep, Bash",
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
            f"The constitution in AGENTS.md applies; read .factory/CONSTITUTION.md (SHA-256 {sha256(constitution)}) only if it is not in your context.\n\n"
            "## Enforced session\n\n"
            "Start this agent as the main session with `claude --agent factory-orchestrator` in a trusted "
            "workspace. The `Agent(...)` allowlist applies only there; frontmatter hooks are skipped until the "
            "workspace trust dialog is accepted and in `-p` sessions. A PreToolUse guard "
            f"(`{GUARD}`) denies Edit, Write, MultiEdit, NotebookEdit, Skill, unknown tools, Agent calls "
            f"for any subagent other than {', '.join(SPECIALISTS)}, and shell commands outside its "
            "read-only allowlist (project-scoped software-factory commands without init, upgrade, uninstall, "
            "recover, render, auth or --root; read-only git; ls/cat/head/tail/wc/grep/rg/find). When a call is denied, delegate the work named in the reason; never try to bypass the "
            "guard. It is a local guardrail: records remain local-unattested and live behaviour is not_run.\n\n"
            + roles["orchestrator"],
        )
    trees = [
        f".{ {'claude': 'claude', 'codex': 'agents', 'copilot': 'github'}[p] }/skills"
        for p in selected_profiles
        if p != "copilot" or len(selected_profiles) == 1
    ]
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
        for asset in asset_names:
            content = source(asset)
            if asset.endswith("/SKILL.md"):
                try:
                    meta, _ = metadata(content.decode())
                except FactoryError as exc:
                    raise FactoryError(f"{exc}: .factory/{asset}") from exc
                if meta["name"] != name:
                    raise FactoryError(f"Skill folder/name mismatch: {name}")
            for tree in trees:
                outputs[tree + "/" + asset.removeprefix("skills/")] = content
    entry_paths = []
    for name in registry["entries"]:
        header, body = metadata(source(f"prompts/{name}.md").decode())
        for tree in trees:
            fields = {"name": name, "description": header["description"]}
            if tree != ".agents/skills":
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
    for name in relinquished:
        del outputs[name]
    for tree in (".claude/skills", ".agents/skills", ".github/skills"):
        base = safe_path(root, tree)
        if base.exists():
            for item in base.glob("*/SKILL.md"):
                rel = item.relative_to(root).as_posix()
                safe_path(root, rel)
                if rel in old or rel in relinquished:
                    continue
                if item.parent.name in names:
                    raise FactoryError(f"Existing skill name collision: {rel}")
                try:
                    meta, _ = metadata(item.read_text())
                except (FactoryError, UnicodeDecodeError):
                    continue  # Unrelated user skill; its format is not the factory's concern.
                if meta["name"] in names:
                    raise FactoryError(f"Existing skill name collision: {rel}")
    changes = {}
    generated = {}
    bases = {name: strip_owned(root, name, record) for name, record in old.items()}
    for name in (
        old.keys() - outputs.keys() - ({".codex/config.toml"} if "codex" in selected_profiles else set())
    ):
        changes[name] = bases[name]
    for name, content in outputs.items():
        base = bases[name] if name in bases else optional(root, name)
        if name in SHARED:
            text = (base or b"").decode()
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
                current = optional(root, name).decode()
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
    if "codex" in selected_profiles:
        name = ".codex/config.toml"
        base = bases[name] if name in bases else optional(root, name)
        try:
            doc = tomlkit.parse((base or b"").decode())
            created_table = "agents" not in doc
            if created_table:
                doc["agents"] = tomlkit.table()
            values = {}
            for key, value in {"enabled": True, "max_concurrent_threads_per_session": 3}.items():
                if key not in doc["agents"]:
                    doc["agents"][key] = value
                    values[key] = value
                elif key == "enabled" and doc["agents"][key] is not True:
                    found = doc["agents"][key]
                    found = found.unwrap() if hasattr(found, "unwrap") else found
                    raise FactoryError(
                        f"Existing Codex setting conflicts: `{name}` [agents].{key} is set by you to "
                        f"{json.dumps(found, default=str)}; the factory needs {json.dumps(value)} — change "
                        "or remove it, then rerun"
                    )
            changes[name] = tomlkit.dumps(doc).encode()
            generated[name] = {
                "kind": "toml",
                "values": values,
                "created_table": created_table,
                "existed": base is not None,
            }
        except (ValueError, TypeError) as exc:
            raise FactoryError(f"Invalid Codex configuration: {exc}") from exc
    config_bytes = (json.dumps(config, indent=2) + "\n").encode()
    existing_config = optional(root, "factory.json")
    if existing_config is not None and json.loads(existing_config) == config:
        config_bytes = existing_config
    source_hashes["factory.json"] = sha256(config_bytes)
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
    if selected is not None or existing_config is None:
        changes["factory.json"] = config_bytes
    return changes, {
        "profiles": selected_profiles,
        "prompt_commands": commands,
        "manifest": manifest,
        "relinquished": sorted(relinquished),
        "pending_relinquish": pending,
        "entry_exports": entry_exports,
    }


def render(root: Path, *, check=False, selected=None, dry_run=False) -> dict:
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
        raise FactoryError("Generated exports are stale: " + ", ".join(drift))
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
        "live_behavior": "not_run",
    }
