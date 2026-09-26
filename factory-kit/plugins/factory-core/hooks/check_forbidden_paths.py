#!/usr/bin/env python3
"""PreToolUse hook: block Write/Edit on forbidden paths (final draft §13, defense in depth).

This is a **second** line of defense, not the primary one. The primary
enforcement is the controller's deterministic action-class classifier
(factory.policy.action_classes.classify), which refuses to push any diff that
touches a forbidden path (AC8) before it ever reaches GitHub. This hook exists
so an agent session gets an immediate, in-loop refusal instead of doing the
work and only discovering it was wasted at push time.

The forbidden-path list below mirrors `factory-kit/policies/floor.yaml`
(`forbidden_paths`). It is duplicated deliberately: this script must stay
stdlib-only and self-contained so it can ship inside the plugin and run in any
product repo without a factory-kit checkout or a Python environment beyond a
bare `python3`. Changing the *authoritative* list is a kit-gate change to
`floor.yaml` (final draft §13.3); update this mirror in the same PR.

Contract (given by the plugin's hooks.json, matcher "Write|Edit"):
  - reads one JSON object from stdin with at least "tool_name" and, nested
    under "tool_input", a "file_path"
  - exit 0 to allow the call to proceed
  - exit 2, with the reason on stderr, to block it

Fails open: if stdin can't be parsed as the expected shape at all, this hook
does not block (a bug here must never brick every edit in every product
repo). It fails **closed** on the one thing it exists to catch: a file_path
that actually matches a forbidden pattern.
"""
from __future__ import annotations

import json
import re
import sys
from functools import lru_cache
from pathlib import Path

# Mirrors factory-kit/policies/floor.yaml `forbidden_paths` (AC8).
FORBIDDEN_PATTERNS = (
    ".github/**",
    "policy/**",
    "policies/**",
    "evals/**",
    "holdouts/**",
    "**/.env",
    "**/.env.*",
    "**/*.pem",
    "**/*.key",
)


@lru_cache(maxsize=64)
def _compile(pattern: str) -> "re.Pattern[str]":
    """Compile a `**`-aware glob pattern to a regex.

    Same semantics as factory.globs.match (`*` within one segment, `**/` zero
    or more directories, a trailing `**` everything below), reimplemented
    here so this file has no dependency on the factory-controller package.
    """
    out = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if pattern.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern.startswith("**", i):
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z")


def matches_any(path: str, patterns) -> str | None:
    """Return the first pattern that matches `path`, or None."""
    for pattern in patterns:
        if _compile(pattern).match(path):
            return pattern
    return None


def relative_path(raw: str, cwd: Path) -> str:
    p = Path(raw)
    if p.is_absolute():
        try:
            return p.relative_to(cwd).as_posix()
        except ValueError:
            return p.as_posix()
    return p.as_posix()


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # fail open: malformed input is a hook bug, not a policy hit

    if not isinstance(payload, dict):
        return 0

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0

    file_path = tool_input.get("file_path")
    if not isinstance(file_path, str) or not file_path:
        return 0

    rel = relative_path(file_path, Path.cwd())
    hit = matches_any(rel, FORBIDDEN_PATTERNS)
    if hit is None:
        return 0

    tool_name = payload.get("tool_name", "this tool")
    print(
        f"Blocked: {tool_name} on {file_path!r} matches the forbidden-path pattern "
        f"{hit!r} (AC8, final draft §13.1 #3). Agents can never change files here; "
        "a human changes them through a normal PR under the kit gate (§13.3). "
        "Report this as a blocker instead of working around it.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
