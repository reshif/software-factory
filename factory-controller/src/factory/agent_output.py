"""Agent output contract: every agent ends with one fenced ```json block (build spec §4).

One implementation for rendering (fakes, tests) and parsing (pipeline, reviewer check).
Parsing is fail-closed: anything unparseable or invalid returns None / raises.
"""
import json
import re
from functools import lru_cache
from pathlib import Path

import jsonschema

_FENCED_JSON = re.compile(r"```json[ \t]*\n(.*?)\n```", re.DOTALL)
ROLES = ("intake", "architect", "implementer", "qa", "reviewer")


class AgentOutputError(ValueError):
    """The agent's final message has no valid output block for its role."""


def render_json_block(payload: dict) -> str:
    return "```json\n" + json.dumps(payload, indent=2) + "\n```"


def parse_last_json_block(text: str) -> dict | None:
    """Return the LAST fenced json object in `text`, or None if absent or not a JSON object."""
    blocks = _FENCED_JSON.findall(text or "")
    if not blocks:
        return None
    try:
        value = json.loads(blocks[-1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


@lru_cache(maxsize=4)
def _schema(kit_dir: str) -> dict:
    return json.loads((Path(kit_dir) / "schemas" / "agent-outputs.schema.json").read_text())


def parse_agent_output(role: str, text: str, *, kit_dir: Path | None = None) -> dict:
    """Parse and validate an agent's output against factory-kit/schemas/agent-outputs.schema.json $defs."""
    if role not in ROLES:
        raise AgentOutputError(f"no output contract for role {role!r}")
    payload = parse_last_json_block(text)
    if payload is None:
        raise AgentOutputError(f"{role}: no parseable ```json block in agent output")
    from .policy.loader import default_kit_dir
    schema = _schema(str(kit_dir or default_kit_dir()))
    definition = schema.get("$defs", {}).get(role)
    if definition is None:
        raise AgentOutputError(f"agent-outputs schema has no $defs.{role}")
    validator = jsonschema.Draft202012Validator({**definition, "$defs": schema["$defs"]})
    errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.path))
    if errors:
        raise AgentOutputError(f"{role}: output violates schema: {errors[0].message}")
    return payload
