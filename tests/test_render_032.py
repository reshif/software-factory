"""0.3.2 rendering hardening: exports, ownership records, vendor settings and the registry."""

import json
import os
import tomllib

import pytest
import yaml

from software_factory.core import FactoryError, sha256
from software_factory.installation import install, uninstall
from software_factory.rendering import (
    GUARD_SELF_TEST,
    ORCHESTRATOR_AGENT,
    enforcement_summary,
    plan_render,
    read_manifest,
    render,
    strip_owned,
)

ENTRIES = {
    "factory-build", "factory-blueprint", "factory-resume", "factory-status", "factory-onboard", "factory-retro",
}  # fmt: skip


def frontmatter(path):
    return yaml.safe_load(path.read_text().split("---\n")[1])


def body(path):
    return path.read_text().split("---\n", 2)[2].strip()


def set_config(root, **changes):
    config = json.loads((root / "factory.json").read_text())
    config.update(changes)
    (root / "factory.json").write_text(json.dumps(config, indent=2) + "\n")


def lock(root):
    return json.loads((root / "factory.lock.json").read_text())


def write_lock(root, data):
    (root / "factory.lock.json").write_text(json.dumps(data, indent=2) + "\n")


# Item 5: stray files under .factory/skills never reach the lock.
def test_editor_backup_in_a_skill_is_skipped_with_a_warning(tmp_path):
    install(tmp_path, selected="claude,codex", skip_sync=True)
    (tmp_path / ".factory/skills/factory-plan/SKILL.md~").write_text("backup\n")
    result = render(tmp_path)
    assert any("SKILL.md~" in warning and "Skipped" in warning for warning in result["warnings"])
    assert not (tmp_path / ".claude/skills/factory-plan/SKILL.md~").exists()
    assert not any(name.endswith("~") for name in lock(tmp_path)["generated"])
    assert read_manifest(tmp_path) is not None
    assert render(tmp_path, check=True)["ok"]
    install(tmp_path, selected="claude,codex", skip_sync=True)  # Upgrade-style plan with a payload.
    assert read_manifest(tmp_path) is not None


# Item 6: a symlinked personal skill directory is skipped by the collision scan.
@pytest.mark.skipif(os.name != "posix", reason="symlinks")
def test_symlinked_user_skill_directory_is_skipped_not_fatal(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    personal = tmp_path.parent / f"{tmp_path.name}-personal"
    (personal / "notes").mkdir(parents=True)
    (personal / "notes/SKILL.md").write_text("---\nname: notes\ndescription: mine\n---\nbody\n")
    (tmp_path / ".claude/skills/notes").symlink_to(personal / "notes", target_is_directory=True)
    _, report = plan_render(tmp_path)
    assert any(".claude/skills/notes/SKILL.md" in warning for warning in report["warnings"])
    # A linked directory that takes a factory skill name is still a collision.
    (tmp_path / ".claude/skills/factory-new").symlink_to(personal / "notes", target_is_directory=True)
    registry = json.loads((tmp_path / ".factory/registry.json").read_text())
    registry["skills"].append("factory-new")
    (tmp_path / ".factory/registry.json").write_text(json.dumps(registry))
    (tmp_path / ".factory/skills/factory-new").mkdir()
    (tmp_path / ".factory/skills/factory-new/SKILL.md").write_text(
        "---\nname: factory-new\ndescription: new\n---\nbody\n"
    )
    with pytest.raises(
        FactoryError, match=r"(collision|Symlink in managed path): .claude/skills/factory-new/SKILL.md"
    ):
        plan_render(tmp_path)


# Item 7: malformed ownership records are FactoryErrors, not crashes.
@pytest.mark.parametrize(
    ("name", "record", "fragment"),
    [
        ("CLAUDE.md", "block", "Invalid renderer ownership"),
        ("CLAUDE.md", None, "Invalid renderer ownership"),
        ("CLAUDE.md", {"kind": "block"}, "fields"),
        ("CLAUDE.md", {"kind": "block", "sha256": 5}, "sha256"),
        ("CLAUDE.md", {"kind": "block", "sha256": "x" * 64}, "sha256"),
        ("CLAUDE.md", {"kind": "block", "sha256": "0" * 64, "separator": "\t"}, "separator"),
        ("CLAUDE.md", {"kind": "block", "sha256": "0" * 64, "existed": "yes"}, "existed"),
        ("CLAUDE.md", {"kind": "block", "sha256": "0" * 64, "extra": 1}, "fields"),
        ("CLAUDE.md", {"kind": "file", "sha256": "0" * 64}, "kind"),
        (".claude/agents/factory-planner.md", {"kind": "file"}, "fields"),
        (".codex/config.toml", {"kind": "toml", "values": []}, "values"),
        (".codex/config.toml", {"kind": "toml", "values": {"model": "x"}}, "TOML ownership key"),
        (".codex/config.toml", {"kind": "toml", "values": {"enabled": "yes"}}, "TOML ownership key"),
        (".codex/config.toml", {"kind": "toml", "values": {}, "appended": "x"}, "appended"),
    ],
)
def test_malformed_lock_records_are_factory_errors(tmp_path, name, record, fragment):
    install(tmp_path, selected="claude,codex", skip_sync=True)
    data = lock(tmp_path)
    data["generated"][name] = record
    write_lock(tmp_path, data)
    with pytest.raises(FactoryError, match=fragment):
        read_manifest(tmp_path)


@pytest.mark.parametrize("data", [[], {"schema_version": 2, "generated": []}, "x"])
def test_malformed_lock_document_is_a_factory_error(tmp_path, data):
    install(tmp_path, selected="claude", skip_sync=True)
    write_lock(tmp_path, data)
    with pytest.raises(FactoryError, match="Unsupported renderer manifest"):
        read_manifest(tmp_path)


def test_pre_032_codex_ownership_record_is_still_accepted_and_removed(tmp_path):
    # 0.3.1 wrote agents.enabled = true; an upgrade must strip it as factory-owned.
    install(tmp_path, selected="codex", skip_sync=True)
    (tmp_path / ".codex/config.toml").write_text(
        "[agents]\nenabled = true\nmax_concurrent_threads_per_session = 3\n"
    )
    data = lock(tmp_path)
    data["generated"][".codex/config.toml"] = {
        "kind": "toml",
        "values": {"enabled": True, "max_concurrent_threads_per_session": 3},
        "created_table": True,
        "existed": False,
    }
    write_lock(tmp_path, data)
    render(tmp_path)
    config = tomllib.loads((tmp_path / ".codex/config.toml").read_text())
    assert config == {"agents": {"max_concurrent_threads_per_session": 3}}
    assert render(tmp_path, check=True)["ok"]


# Item 8: TOML stripping never crashes on a scalar agents and restores the user's bytes.
def test_scalar_agents_is_reported_as_drift(tmp_path):
    install(tmp_path, selected="codex", skip_sync=True)
    record = lock(tmp_path)["generated"][".codex/config.toml"]
    (tmp_path / ".codex/config.toml").write_text("agents = 3\n")
    with pytest.raises(FactoryError, match="Generated TOML key drift"):
        strip_owned(tmp_path, ".codex/config.toml", record)


def test_scalar_agents_without_ownership_is_a_configuration_error(tmp_path):
    path = tmp_path / ".codex/config.toml"
    path.parent.mkdir(parents=True)
    path.write_text("agents = 3\n")
    with pytest.raises(FactoryError, match="agents must be a table"):
        install(tmp_path, selected="codex", skip_sync=True)
    assert path.read_text() == "agents = 3\n"


@pytest.mark.parametrize(
    "original",
    [
        b'model = "example"\n',
        b'model = "example"',
        b'# comment\nmodel = "example"\n\n[other]\na = 1\n',
        b"",
        b"[agents]\nother = 1\n",
    ],
)
def test_codex_config_round_trips_byte_for_byte(tmp_path, original):
    path = tmp_path / ".codex/config.toml"
    path.parent.mkdir(parents=True)
    path.write_bytes(original)
    install(tmp_path, selected="codex", skip_sync=True)
    text = path.read_text()
    assert "enabled" not in tomllib.loads(text).get("agents", {})
    assert tomllib.loads(text)["agents"]["max_concurrent_threads_per_session"] == 3
    assert render(tmp_path, check=True)["ok"]
    uninstall(tmp_path)
    assert (path.read_bytes() if path.exists() else b"") == original


# Item 9: Codex configuration rules.
@pytest.mark.parametrize("existing", ["max_threads = 2", "max_concurrent_threads_per_session = 5"])
def test_existing_thread_limit_satisfies_codex(tmp_path, existing):
    original = f'model = "x"\n\n[agents]\n{existing}\n'.encode()
    path = tmp_path / ".codex/config.toml"
    path.parent.mkdir(parents=True)
    path.write_bytes(original)
    install(tmp_path, selected="codex", skip_sync=True)
    assert path.read_bytes() == original
    assert lock(tmp_path)["generated"][".codex/config.toml"]["values"] == {}
    uninstall(tmp_path)
    assert path.read_bytes() == original


@pytest.mark.parametrize("text", ["[features]\nmulti_agent = false\n", "features.multi_agent = false\n"])
def test_disabled_multi_agent_is_an_error_with_guidance(tmp_path, text):
    path = tmp_path / ".codex/config.toml"
    path.parent.mkdir(parents=True)
    path.write_text(text)
    with pytest.raises(FactoryError, match="features.multi_agent = false disables the subagents"):
        install(tmp_path, selected="codex", skip_sync=True)
    assert path.read_text() == text


def test_codex_agents_md_names_the_venv_entrypoint_and_size_is_warned(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    assert ".factory/.venv/bin/software-factory" in (tmp_path / "AGENTS.md").read_text()
    assert "Codex sandboxes" not in (tmp_path / "AGENTS.md").read_text()
    render(tmp_path, selected="codex")
    assert "Codex sandboxes cannot write the uv cache" in (tmp_path / "AGENTS.md").read_text()
    assert "main session only" in (tmp_path / "AGENTS.md").read_text()
    assert render(tmp_path, dry_run=True)["warnings"] == []
    with (tmp_path / "AGENTS.md").open("a") as handle:
        handle.write("\n" + "user rule\n" * 4000)
    warnings = render(tmp_path, dry_run=True)["warnings"]
    assert any("AGENTS.md is" in w and "32768" in w for w in warnings)


# Item 10: Claude exports.
def test_claude_tools_self_test_and_hidden_skills(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    set_config(tmp_path, enforcement={"claude_orchestrator_agent": True})
    render(tmp_path)
    implementer = frontmatter(tmp_path / ".claude/agents/factory-implementer.md")
    assert "WebFetch" in implementer["tools"] and "WebSearch" in implementer["tools"]
    orchestrator = frontmatter(tmp_path / ORCHESTRATOR_AGENT)
    assert orchestrator["tools"].endswith("Bash, AskUserQuestion, TodoWrite")
    assert body(tmp_path / ORCHESTRATOR_AGENT).startswith(GUARD_SELF_TEST)
    assert GUARD_SELF_TEST.startswith("First run Bash `true`. The guard must deny it.")
    assert GUARD_SELF_TEST not in (tmp_path / ".factory/roles/orchestrator.md").read_text()
    for skill in (tmp_path / ".claude/skills").iterdir():
        header = frontmatter(skill / "SKILL.md")
        assert header["user-invocable"] is (skill.name in ENTRIES), skill.name
    assert "main session only" in (tmp_path / "AGENTS.md").read_text()
    # The rewritten skill keeps its canonical body.
    canonical = (tmp_path / ".factory/skills/factory-plan/SKILL.md").read_text()
    assert body(tmp_path / ".claude/skills/factory-plan/SKILL.md") == canonical.split("---\n", 2)[2].strip()


# Item 11: Copilot exports.
def test_copilot_agent_tools_preamble_and_supported_environments(tmp_path):
    install(tmp_path, selected="copilot", skip_sync=True)
    factory = tmp_path / ".github/agents/factory.agent.md"
    assert frontmatter(factory)["tools"] == ["read", "search", "execute", "agent"]
    assert body(factory).startswith(
        "The constitution in AGENTS.md applies; read .factory/CONSTITUTION.md (SHA-256"
    )
    for role in ("planner", "implementer", "verifier", "reviewer"):
        header = frontmatter(tmp_path / f".github/agents/factory-{role}.agent.md")
        assert header["include-custom-instructions"] is True and header["user-invocable"] is False
    instructions = (tmp_path / ".github/copilot-instructions.md").read_text()
    assert (
        "VS Code Local" in instructions
        and "Copilot CLI and the Copilot cloud agent are not supported" in instructions
    )
    for skill in (tmp_path / ".github/skills").iterdir():
        assert frontmatter(skill / "SKILL.md")["user-invocable"] is (skill.name in ENTRIES)


# Item 12: .agents/skills carry Copilot slash-command keys only when Copilot reads them.
def test_agents_skills_carry_copilot_keys_only_with_copilot(tmp_path):
    install(tmp_path, selected="codex", skip_sync=True)
    header = frontmatter(tmp_path / ".agents/skills/factory-build/SKILL.md")
    assert set(header) == {"name", "description"}
    assert "user-invocable" not in frontmatter(tmp_path / ".agents/skills/factory-plan/SKILL.md")
    render(tmp_path, selected="codex,copilot")
    header = frontmatter(tmp_path / ".agents/skills/factory-build/SKILL.md")
    assert header["user-invocable"] is True and header["argument-hint"]
    assert frontmatter(tmp_path / ".agents/skills/factory-plan/SKILL.md")["user-invocable"] is False
    assert (tmp_path / ".agents/skills/factory-build/agents/openai.yaml").is_file()


# Item 13: decoding and prompt fields.
@pytest.mark.parametrize("name", ["AGENTS.md", "CLAUDE.md"])
def test_non_utf8_shared_file_names_the_file(tmp_path, name):
    (tmp_path / name).write_bytes(b"caf\xe9\n")
    with pytest.raises(FactoryError, match=f"{name} is not valid UTF-8"):
        install(tmp_path, selected="claude", skip_sync=True)


def test_non_utf8_owned_file_names_the_file_on_uninstall_and_render(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    path = tmp_path / "CLAUDE.md"
    path.write_bytes(path.read_bytes() + b"\xff\n")
    with pytest.raises(FactoryError, match="CLAUDE.md is not valid UTF-8"):
        render(tmp_path)


@pytest.mark.parametrize("field", ["argument-hint", "default-prompt"])
def test_prompt_without_required_field_is_a_factory_error(tmp_path, field):
    install(tmp_path, selected="claude", skip_sync=True)
    prompt = tmp_path / ".factory/prompts/factory-status.md"
    prompt.write_text(
        "\n".join(line for line in prompt.read_text().split("\n") if not line.startswith(field + ":"))
    )
    with pytest.raises(FactoryError, match=f"factory-status.md needs a nonempty {field}"):
        render(tmp_path)


# Item 14: the lock hashes factory.json as it is on disk.
def test_factory_json_source_hash_is_the_on_disk_file(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    config = json.loads((tmp_path / "factory.json").read_text())
    config["profile"] = ["claude"]  # Normalised to "claude" in memory, but not rewritten.
    (tmp_path / "factory.json").write_text(json.dumps(config, indent=4) + "\n")
    render(tmp_path)
    assert lock(tmp_path)["sources"]["factory.json"] == sha256((tmp_path / "factory.json").read_bytes())
    assert render(tmp_path, check=True)["ok"]


# Item 15: relinquished exports across profile switches; uninstalled check; CRLF.
def test_relinquished_user_file_survives_a_profile_switch(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    name = ".claude/agents/factory-planner.md"
    (tmp_path / name).unlink()
    install(tmp_path, selected="claude", skip_sync=True)
    assert name in lock(tmp_path)["relinquished"]
    (tmp_path / name).write_text("my own planner\n")
    render(tmp_path, selected="codex")
    assert name in lock(tmp_path)["relinquished"]
    render(tmp_path, selected="claude")  # No "Unowned file collision".
    assert (tmp_path / name).read_text() == "my own planner\n"
    assert render(tmp_path, check=True)["ok"]


def test_render_check_on_uninstalled_project_says_uninstalled(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    uninstall(tmp_path)
    with pytest.raises(FactoryError, match="uninstalled"):
        render(tmp_path, check=True)


def test_drift_message_mentions_line_endings(tmp_path):
    install(tmp_path, selected="claude", skip_sync=True)
    path = tmp_path / "CLAUDE.md"
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    with pytest.raises(FactoryError, match="CRLF"):
        render(tmp_path, check=True)


# Item 16: registry rules.
@pytest.mark.parametrize(
    ("change", "fragment"),
    [
        (lambda r: r["roles"][0].update(name="orchestrator"), "orchestrator is reserved"),
        (lambda r: r["entries"].append("factory-plan"), "unique across"),
        (lambda r: r["skills"].append("factory-status"), "unique across"),
        (lambda r: r["skills"].append("factory-planner"), "unique across"),
        (lambda r: r["roles"].append({**r["roles"][0], "name": "extra"}), "Invalid registry"),
        (lambda r: r["skills"].remove("factory-review"), "missing required skills"),
        (lambda r: r["entries"].remove("factory-status"), "Invalid registry"),
        (lambda r: r["roles"][1].update(name="coder"), "exactly the factory specialists"),
    ],
)
def test_registry_rules(tmp_path, change, fragment):
    install(tmp_path, selected="claude", skip_sync=True)
    path = tmp_path / ".factory/registry.json"
    registry = json.loads(path.read_text())
    change(registry)
    path.write_text(json.dumps(registry))
    with pytest.raises(FactoryError, match=fragment):
        plan_render(tmp_path)


# Item 18: enforcement reflects what is exported.
def test_enforcement_enabled_reflects_the_exported_agent(tmp_path):
    install(tmp_path, selected="claude,copilot", skip_sync=True)
    config = json.loads((tmp_path / "factory.json").read_text())
    config["enforcement"] = {"claude_orchestrator_agent": True}
    (tmp_path / "factory.json").write_text(json.dumps(config, indent=2) + "\n")
    summary = enforcement_summary(tmp_path, config)
    assert summary["claude"]["configured"] is True and summary["claude"]["enabled"] is False
    assert summary["claude"]["layer"] == "instructions"
    assert summary["copilot"]["enabled"] is True and summary["copilot"]["layer"] == "tool_allowlist"
    render(tmp_path)
    summary = enforcement_summary(tmp_path, config)
    assert summary["claude"]["enabled"] is True and summary["claude"]["layer"] == "hook"
    (tmp_path / ORCHESTRATOR_AGENT).unlink()
    assert enforcement_summary(tmp_path, config)["claude"]["enabled"] is False


# Vendor capability wording (item 17).
def test_vendor_capabilities_are_accurate():
    from software_factory.core import asset_root

    claude = json.loads((asset_root() / "vendors/claude.json").read_text())
    codex = json.loads((asset_root() / "vendors/codex.json").read_text())
    copilot = json.loads((asset_root() / "vendors/copilot.json").read_text())
    assert claude["capabilities"]["blocking_hooks"] == "fail_closed_when_run"
    assert any("fails closed only when it runs" in limit for limit in claude["limits"])
    assert codex["reviewed_on"] == "2026-09-28"
    assert not any("may ignore" in limit for limit in codex["limits"])
    assert any("0.114" in limit and "reject" in limit for limit in codex["limits"])
    assert any("Future work: Codex hooks" in limit for limit in codex["limits"])
    assert any("VS Code Local" in limit and "agents allowlist" in limit for limit in copilot["limits"])
    assert any("agent-scoped hooks" in limit for limit in copilot["limits"])


# CLAUDE.md/AGENTS.md separator: no leftover blank line when user text follows the section.
@pytest.mark.parametrize(("original", "expected"), [(b"User\n", b"User\nMore\n"), (b"User", b"User\nMore\n")])
def test_section_removal_drops_the_separator_before_trailing_user_text(tmp_path, original, expected):
    (tmp_path / "AGENTS.md").write_bytes(original)
    install(tmp_path, selected="codex", skip_sync=True)
    with (tmp_path / "AGENTS.md").open("ab") as handle:
        handle.write(b"More\n")
    uninstall(tmp_path)
    assert (tmp_path / "AGENTS.md").read_bytes() == expected
