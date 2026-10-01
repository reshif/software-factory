"""Which model each factory role uses, per client, and whether the user confirmed it (0.3.9).

With ``model_selection.mode: "roles"`` every role's agent file pins a model (Claude ``model:``
frontmatter, Codex agent TOML ``model``, Copilot ``.agent.md`` ``model``), so the client itself runs
that role on it. The map is the user's decision: the orchestrator shows the choices at the first
``factory-build``, proposes a map as a setup proposal, and the user approves it in one line. Applying
it stamps ``confirmed_sha256``, the hash of the approved map, so an edited map counts as unconfirmed
until it is approved again. Each mission binds the map it started with, and the gate refuses a
mission whose map changed underneath it.

Choices come from the reviewed guidance in ``.factory/models/recommendations.json``; nothing here
asks a provider which models an account can use, so availability is the user's to confirm.
"""

from __future__ import annotations

import json

from .core import FactoryError, digest, profiles

ROLES = ("orchestrator", "planner", "implementer", "verifier", "reviewer")
# What each role needs: deep judgment, everyday coding, or narrow and fast work.
TIER = {
    "orchestrator": "deep",
    "planner": "deep",
    "reviewer": "deep",
    "implementer": "coding",
    "verifier": "light",
}
# The reviewed guidance entry each client uses for a tier.
TIER_ENTRY = {
    "claude": {"deep": "anthropic-opus", "coding": "anthropic-sonnet", "light": "anthropic-haiku"},
    "codex": {"deep": "openai-astra", "coding": "openai-sol", "light": "openai-luna"},
    "copilot": {"deep": "anthropic-opus", "coding": "anthropic-sonnet", "light": "anthropic-haiku"},
}
NAMES = {
    "claude": "Claude Code: an alias (opus, sonnet, haiku, fable) always runs that family's newest model",
    "codex": "Codex: the model ID your Codex shows (codex /model)",
    "copilot": "Copilot in VS Code: the model name as the Copilot model picker shows it",
}


def _entries(root=None) -> list[dict]:
    from .models import model_controls

    try:
        return model_controls(root)["recommendations"]["entries"] if root else _packaged()
    except (FactoryError, OSError, KeyError, ValueError):
        return _packaged()


def _packaged() -> list[dict]:
    from .core import asset_root

    return json.loads((asset_root() / "models/recommendations.json").read_text())["entries"]


def _value(client: str, entry: dict) -> str:
    """How the client names the model: Claude aliases, Codex IDs, Copilot display names."""
    if client == "claude" and entry["key"].startswith("anthropic-"):
        return entry["key"].split("-", 1)[1]
    if client == "copilot":
        return entry["display_names"][0]
    return entry["model_ids"][0]


def choices(client: str, root=None) -> list[dict]:
    return [
        {"model": _value(client, e), "name": e["display_names"][0], "good_for": e["strengths"]}
        for e in _entries(root)
        if client in e.get("profiles", [])
    ]


def default_roles(client: str, root=None) -> dict[str, str]:
    by_key = {e["key"]: e for e in _entries(root)}
    return {role: _value(client, by_key[TIER_ENTRY[client][TIER[role]]]) for role in ROLES}


def default_selection(selected) -> dict:
    return {"mode": "roles", "roles": {client: default_roles(client) for client in profiles(selected)}}


def map_hash(roles: dict) -> str:
    return digest({client: dict(sorted(roles[client].items())) for client in sorted(roles)})


def stamped(selection: dict) -> dict:
    """The selection a setup proposal writes: roles mode carries the hash of the approved map."""
    selection = {k: v for k, v in selection.items() if k != "confirmed_sha256"}
    if selection.get("mode") == "roles":
        selection["confirmed_sha256"] = map_hash(selection.get("roles") or {})
    return selection


def problems(config: dict) -> list[str]:
    """Why the model choice is not settled for a mission (empty when it is)."""
    selection = config.get("model_selection") or {}
    mode = selection.get("mode", "inherit")
    if mode in ("recommend", "required"):
        return []  # Planned per task by `models plan`.
    if mode != "roles":
        return ["no model is chosen per role (model_selection is inherit)"]
    roles = selection.get("roles") or {}
    missing = [c for c in profiles(config) if not roles.get(c) or set(ROLES) - set(roles[c])]
    if missing:
        return ["no model is chosen for every role of " + ", ".join(missing)]
    if selection.get("confirmed_sha256") != map_hash(roles):
        return ["the model for each role is not confirmed by the user yet"]
    return []


def binding(config: dict) -> dict | None:
    """What a mission records about its models: the map and its hash (roles mode only)."""
    selection = config.get("model_selection") or {}
    if selection.get("mode") != "roles":
        return None
    roles = {c: dict((selection.get("roles") or {}).get(c) or {}) for c in profiles(config)}
    return {"roles": roles, "sha256": map_hash(roles), "confirmed": not problems(config)}


def status(root, config: dict) -> dict:
    """What the orchestrator shows the user at startup, and the proposal that settles it."""
    selection = config.get("model_selection") or {}
    clients = profiles(config)
    current = (selection.get("roles") or {}) if selection.get("mode") == "roles" else {}
    suggested = {c: {**default_roles(c, root), **(current.get(c) or {})} for c in clients}
    open_problems = problems(config)
    report = {
        "mode": selection.get("mode", "inherit"),
        "confirmed": not open_problems,
        "clients": {
            client: {
                "roles": current.get(client) or {},
                "how_to_name": NAMES[client],
                "choices": choices(client, root),
            }
            for client in clients
        },
        "availability": "From reviewed guidance; nothing asked your provider. The user confirms what their "
        "account offers.",
    }
    if open_problems:
        report["problems"] = open_problems
        report["proposal"] = {
            "reason": "Choose which model each factory role uses",
            "model_selection": {"mode": "roles", "roles": suggested},
        }
        report["next"] = (
            "Show the user one line per role and client (role: model) with the choices, take their changes, then "
            "pipe the proposal to `software-factory setup propose --input -` and ask for `approve S-n setup`"
        )
    return report
