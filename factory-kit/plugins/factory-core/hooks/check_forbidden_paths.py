#!/usr/bin/env python3
"""PreToolUse hook: block Write/Edit/MultiEdit/NotebookEdit on forbidden paths
(final draft §13, defense in depth).

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
`factory-controller/tests/kit/test_hooks.py` asserts the two stay in sync.

Contract (given by the plugin's hooks.json, matcher
"Write|Edit|MultiEdit|NotebookEdit"):
  - reads one JSON object from stdin with at least "tool_name" and, nested
    under "tool_input", the path the tool would write to ("file_path" for
    Write/Edit/MultiEdit, "notebook_path" for NotebookEdit)
  - exit 0 to allow the call to proceed
  - exit 2, with the reason on stderr, to block it

Fail-closed for the tools this hook guards (red-team finding R-A6): stdin that
doesn't parse, a payload that isn't the expected shape, or a target path that
doesn't resolve to somewhere inside the project root are all treated as a
**block**, not a pass-through. A bug in this script must never turn into a
silent bypass of the forbidden-path check for a Write-shaped tool call. Tool
calls this hook was not asked to guard (anything outside `TARGET_TOOLS`) are
out of scope and always allowed through, regardless of payload shape.
"""
from __future__ import annotations

import json
import os
import re
import sys
from functools import lru_cache
from pathlib import Path

# Mirrors factory-kit/policies/floor.yaml `forbidden_paths` (AC8). Keep lowercase:
# matching is case-insensitive (R-A6: `.GitHub/**`-style bypasses).
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
    "factory.yaml",
    "mandates/**",
    "**/CODEOWNERS",
    "**/.claude/**",
    "**/CLAUDE.md",
    "**/AGENTS.md",
)

# The tools this hook is wired to via hooks.json's matcher. Anything else is
# out of scope for this hook and passes through untouched.
TARGET_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})


@lru_cache(maxsize=64)
def _compile(pattern: str) -> "re.Pattern[str]":
    """Compile a `**`-aware glob pattern to a regex.

    Algorithmically identical to factory.globs._compile (`*` within one
    segment, `**/` zero or more directories, a trailing `**` everything
    below), reimplemented here so this file has no dependency on the
    factory-controller package. `test_hooks.py` checks the two agree over a
    path x pattern matrix.
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


def glob_match(path: str, pattern: str) -> bool:
    """Case-sensitive `**`-glob match — the same algorithm as factory.globs.match."""
    return _compile(pattern).match(path) is not None


def matches_any_case_insensitive(path: str, patterns) -> str | None:
    """Return the first pattern that matches `path`, ignoring case, or None.

    Case-insensitive so a path component spelled `.GitHub` or `.ENV` can't
    walk past a check written for `.github`/`.env` (R-A6).
    """
    lowered = path.lower()
    for pattern in patterns:
        if glob_match(lowered, pattern.lower()):
            return pattern
    return None


def project_root() -> Path:
    """The directory forbidden-path checks are relative to.

    `CLAUDE_PROJECT_DIR` is the project root Claude Code sets for hooks; fall
    back to cwd (what the hook receives when that isn't set, e.g. in tests).
    Resolved so a symlinked root doesn't defeat the containment check below.
    """
    raw = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    return Path(raw).resolve()


def resolve_relative(raw_path: str, root: Path) -> str | None:
    """Resolve `raw_path` (absolute or relative, possibly with `..` segments)
    against `root` and return its POSIX path relative to `root`.

    Returns None when the path can't be resolved at all, or when it resolves
    to somewhere outside `root` — both are treated as "unresolvable" by the
    caller and fail closed (R-A6): an absolute path that looks rooted
    elsewhere (e.g. a container path like `/work/...` that isn't actually
    this project's root) must not fall through as an unmatched raw string the
    way a naive `relative_to` fallback used to.
    """
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return None


def _extract_target_path(tool_name: str, tool_input: dict) -> object:
    if tool_name == "NotebookEdit":
        return tool_input.get("notebook_path") or tool_input.get("file_path")
    return tool_input.get("file_path")


def _block(reason: str) -> int:
    print(f"Blocked: {reason}", file=sys.stderr)
    return 2


def main() -> int:
    try:
        raw_stdin = sys.stdin.read()
        payload = json.loads(raw_stdin)
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        # A guarded tool call with input this hook can't even parse is exactly
        # the situation R-A6 flagged: fail closed rather than let it through.
        return _block(
            "could not parse hook input as JSON on a Write/Edit/MultiEdit/NotebookEdit "
            "call; failing closed (AC8, final draft §13.1)."
        )

    if not isinstance(payload, dict):
        return _block("hook input was not a JSON object; failing closed.")

    tool_name = payload.get("tool_name")
    if tool_name not in TARGET_TOOLS:
        return 0  # out of scope for this hook

    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return _block(f"{tool_name}: tool_input missing or malformed; failing closed.")

    raw_path = _extract_target_path(tool_name, tool_input)
    if not isinstance(raw_path, str) or not raw_path:
        return _block(f"{tool_name}: no usable path in tool_input; failing closed.")

    root = project_root()
    rel = resolve_relative(raw_path, root)
    if rel is None:
        return _block(
            f"{tool_name} target {raw_path!r} does not resolve to a path inside the "
            f"project root ({root}); failing closed rather than guess."
        )

    hit = matches_any_case_insensitive(rel, FORBIDDEN_PATTERNS)
    if hit is None:
        return 0

    return _block(
        f"{tool_name} on {raw_path!r} (resolved: {rel!r}) matches the forbidden-path "
        f"pattern {hit!r} (AC8, final draft §13.1 #3). Agents can never change files "
        "here; a human changes them through a normal PR under the kit gate (§13.3). "
        "Report this as a blocker instead of working around it."
    )


if __name__ == "__main__":
    sys.exit(main())
