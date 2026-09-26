#!/usr/bin/env python3
"""Render CLAUDE.md and AGENTS.md for a product from base.md + its factory.yaml.

Two agent harnesses, one source of truth: Claude Code reads `CLAUDE.md`, other
tools read the emerging `AGENTS.md` convention. Keeping them hand-written
separately lets them drift; rendering both from the single `base.md` template
plus the product's `factory.yaml` (final draft §12.4) means they can't.

Usage:
    python render.py <path/to/factory.yaml> [--base BASE_MD] [--out-dir DIR]

By default the template is the `base.md` next to this script, and the two
output files are written alongside the given `factory.yaml`.

Placeholders in `base.md` use `{{name}}` and are substituted from a flat
context built by `build_context()` below. An unknown placeholder is a
programming error in `base.md` (or a stale one after a schema change) and
raises, rather than silently leaving `{{...}}` in the rendered guide.

Requires PyYAML and jsonschema (both already used elsewhere in this repo):
`factory.yaml` is validated against `factory-kit/schemas/factory.schema.json`
before anything is rendered from it, so a broken profile fails loudly here
instead of quietly producing guidance that looks fine but reflects a document
the rest of the factory would actually reject.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable

import jsonschema
import yaml

PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z0-9_]+)\s*\}\}")
SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schemas" / "factory.schema.json"


class FactoryYamlError(ValueError):
    """`factory.yaml` doesn't validate against factory.schema.json."""


def _bullets(items: Iterable[str], empty: str = "(none declared)") -> str:
    items = list(items)
    if not items:
        return empty
    return "\n".join(f"- {item}" for item in items)


def _money(value: float) -> str:
    return f"${value:g}"


def _approver_line(gate: str, value) -> str:
    shown = "CODEOWNERS" if value == "codeowners" else ", ".join(value)
    return f"{gate.upper()}: {shown}"


def _budget_line(key: str, value) -> str:
    return f"{key}: {_money(value)}" if key.endswith("_usd") else f"{key}: {value}"


def build_context(profile: dict) -> dict:
    """Flatten a validated factory.yaml document into base.md placeholders."""
    owners = profile["owners"]
    approvers = profile["approvers"]
    budgets = profile["budgets"]
    verification = profile["verification"]

    return {
        "product": profile["product"],
        "risk_profile": profile["risk_profile"],
        "autonomy_level": profile["autonomy_level"],
        "runtime": profile["runtime"],
        "kit_version": profile["kit"]["version"],
        "policy_version": profile["kit"]["policy_version"],
        "lanes": ", ".join(profile["lanes"]),
        "owners_lines": _bullets(f"{role}: {who}" for role, who in owners.items()),
        "approvers_lines": _bullets(
            _approver_line(gate, approvers[gate]) for gate in ("h1", "hm", "h2", "hx")
        ),
        "protected_paths_lines": _bullets(profile.get("protected_paths", [])),
        "forbidden_paths_lines": _bullets(profile.get("forbidden_paths", [])),
        "verification_required": ", ".join(verification["required"]),
        "mutation": verification.get("mutation", "off"),
        "budgets_lines": _bullets(_budget_line(k, v) for k, v in budgets.items()),
        "holdout_repo": profile.get("holdout_repo", "(not configured)"),
        "observation_window": profile.get("observation_window", "(not configured)"),
    }


def render(template: str, context: dict) -> str:
    def substitute(match: re.Match) -> str:
        key = match.group(1)
        if key not in context:
            raise KeyError(f"base.md uses unknown placeholder {{{{{key}}}}}")
        return str(context[key])

    return PLACEHOLDER.sub(substitute, template)


def validate_factory_yaml(profile: dict, schema_path: Path = SCHEMA_PATH) -> None:
    """Raise FactoryYamlError if `profile` doesn't match factory.schema.json.

    Run before build_context() ever touches the document: build_context does
    plain dict indexing (`profile["owners"]`, ...) and would raise a confusing
    KeyError on a malformed profile instead of a clear schema error.
    """
    schema_doc = json.loads(Path(schema_path).read_text())
    validator = jsonschema.Draft202012Validator(schema_doc, format_checker=jsonschema.FormatChecker())
    errors = sorted(validator.iter_errors(profile), key=lambda e: list(e.path))
    if errors:
        details = "; ".join(f"{list(e.path)}: {e.message}" for e in errors)
        raise FactoryYamlError(f"factory.yaml does not match factory.schema.json: {details}")


def render_product(factory_yaml: Path, base_md: Path, *, schema_path: Path = SCHEMA_PATH) -> str:
    profile = yaml.safe_load(factory_yaml.read_text())
    validate_factory_yaml(profile, schema_path)
    template = base_md.read_text()
    return render(template, build_context(profile))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("factory_yaml", type=Path, help="Path to the product's factory.yaml")
    parser.add_argument(
        "--base", type=Path, default=Path(__file__).with_name("base.md"),
        help="Template to render (default: base.md next to this script)",
    )
    parser.add_argument(
        "--out-dir", type=Path, default=None,
        help="Directory to write CLAUDE.md/AGENTS.md into (default: next to factory.yaml)",
    )
    args = parser.parse_args(argv)

    try:
        rendered = render_product(args.factory_yaml, args.base)
    except FactoryYamlError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    out_dir = args.out_dir or args.factory_yaml.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "CLAUDE.md").write_text(rendered)
    (out_dir / "AGENTS.md").write_text(rendered)
    print(f"wrote {out_dir / 'CLAUDE.md'} and {out_dir / 'AGENTS.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
