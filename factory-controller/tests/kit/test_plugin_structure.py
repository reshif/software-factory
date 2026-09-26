"""factory-kit/plugins/factory-core: plugin manifest, agents and skills (B6).

Spec: docs/build/phase2-build-spec.md §3 (B6), final draft §11-13.
"""
import json
import re

from factory.pipeline.tool_guard import ALLOWED_TOOLS

from .conftest import parse_frontmatter

EXPECTED_AGENTS = {"intake", "architect", "coordinator", "implementer", "qa", "reviewer"}
EXPECTED_SKILLS = {
    "write-spec-ears", "plan-task-graph", "tdd-implement",
    "build-decision-packet", "write-recovery-plan",
}
VALID_MODELS = {"haiku", "sonnet", "opus", "fable", "inherit"}


def test_plugin_manifest_has_required_fields(plugin_dir):
    manifest = json.loads((plugin_dir / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == "factory-core"
    assert manifest["version"]
    assert manifest["description"]


def test_marketplace_lists_factory_core(kit_dir):
    marketplace = json.loads((kit_dir / "plugins" / ".claude-plugin" / "marketplace.json").read_text())
    names = {p["name"] for p in marketplace["plugins"]}
    assert "factory-core" in names
    entry = next(p for p in marketplace["plugins"] if p["name"] == "factory-core")
    assert (kit_dir / "plugins" / entry["source"].lstrip("./")).is_dir()


def test_all_six_agents_present(plugin_dir):
    agent_files = {p.stem for p in (plugin_dir / "agents").glob("*.md")}
    assert agent_files == EXPECTED_AGENTS


def test_agent_frontmatter_is_complete(plugin_dir):
    for path in sorted((plugin_dir / "agents").glob("*.md")):
        frontmatter = parse_frontmatter(path)
        for key in ("name", "description", "tools", "model"):
            assert key in frontmatter and frontmatter[key], f"{path.name} missing {key!r}"
        assert frontmatter["name"] == path.stem
        tools = [t.strip() for t in frontmatter["tools"].split(",")]
        assert all(tools), f"{path.name} has an empty tool entry"
        assert frontmatter["model"] in VALID_MODELS, f"{path.name} has an unknown model {frontmatter['model']!r}"


UNTRUSTED_DATA_PATTERN = re.compile(r"as data|data, not|treat.*data", re.IGNORECASE)
PROTECTED_RECORDS_PATTERN = re.compile(
    r"AC6|AC8|protected path|forbidden path|existing test|fixture|conftest", re.IGNORECASE
)


def test_agent_prompts_enforce_the_security_floor(plugin_dir):
    """Every agent prompt must cite the non-negotiable §13 rules (spec §3 B6):
    untrusted text is data, and existing tests/fixtures/CI/policies are off limits."""
    for path in sorted((plugin_dir / "agents").glob("*.md")):
        body = path.read_text()
        assert UNTRUSTED_DATA_PATTERN.search(body), f"{path.name} doesn't say untrusted text is data"
        assert PROTECTED_RECORDS_PATTERN.search(body), f"{path.name} doesn't mention protected records"


def test_agent_prompts_end_with_a_json_output_block(plugin_dir):
    for path in sorted((plugin_dir / "agents").glob("*.md")):
        text = path.read_text()
        assert "## Output" in text, f"{path.name} has no Output section"
        assert "```json" in text, f"{path.name} has no example JSON output block"


def test_no_agent_frontmatter_grants_bash(plugin_dir):
    """The pipeline's ToolGuard (factory.pipeline.tool_guard.ALLOWED_TOOLS)
    denies Bash outright for every role -- only the controller ever runs
    commands, through its own sandboxed exec. Granting Bash in an agent's
    frontmatter would be a dead, misleading permission: the SDK would still
    offer the tool, `on_tool_approval` would still refuse every call."""
    for path in sorted((plugin_dir / "agents").glob("*.md")):
        frontmatter = parse_frontmatter(path)
        tools = [t.strip() for t in frontmatter["tools"].split(",")]
        assert "Bash" not in tools, f"{path.name} grants Bash, which the controller's tool guard always denies"


def test_no_agent_frontmatter_grants_a_tool_the_guard_would_deny(plugin_dir):
    """Generalizes the Bash check to every tool: an agent's `tools:` frontmatter
    should never promise more than factory.pipeline.tool_guard.ALLOWED_TOOLS
    will actually let through at runtime (Read, Glob, Grep, Edit, Write)."""
    for path in sorted((plugin_dir / "agents").glob("*.md")):
        frontmatter = parse_frontmatter(path)
        tools = {t.strip() for t in frontmatter["tools"].split(",")}
        extra = tools - ALLOWED_TOOLS
        assert not extra, f"{path.name} grants {extra}, which the controller's tool guard always denies"


def test_implementer_and_qa_explain_the_controller_runs_checks(plugin_dir):
    for name in ("implementer", "qa"):
        body = (plugin_dir / "agents" / f"{name}.md").read_text()
        assert "no `Bash`" in body or "no `bash`" in body.lower()
        assert "controller" in body.lower() and ("runs" in body.lower() or "ran" in body.lower())


def test_all_five_skills_present(plugin_dir):
    skill_dirs = {p.parent.name for p in (plugin_dir / "skills").glob("*/SKILL.md")}
    assert skill_dirs == EXPECTED_SKILLS


def test_skill_frontmatter_is_complete(plugin_dir):
    for path in sorted((plugin_dir / "skills").glob("*/SKILL.md")):
        frontmatter = parse_frontmatter(path)
        assert frontmatter.get("name") == path.parent.name
        assert frontmatter.get("description")
