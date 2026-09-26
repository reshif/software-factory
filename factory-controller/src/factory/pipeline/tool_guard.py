"""The tool guard: every agent tool call goes through `on_tool_approval` (build spec §3 B7).

Agents run with `allowed_tools=()` (final draft §12.3), so the SDK's `can_use_tool`
callback calls `on_tool_approval(tool_name, tool_input)` for **every** tool call, with
no exceptions. This module is that callback:

  - `Read`, `Glob`, `Grep`, `Edit`, `Write` are allowed only when every path they touch
    resolves (symlinks and `..` included) to somewhere inside the task's sandbox
    workdir.
  - `Edit` and `Write` are allowed only when that resolved path is also inside the
    task's `owned_paths`, and is not a floor or product protected/forbidden path.
  - Everything else -- `Bash` above all -- is denied outright. **The controller runs
    code, not the agent**: checks run through `SandboxPort.exec(network=False)`, and
    their trimmed output feeds the repair prompt.

v1 containment limitation (documented, not hidden): this guard filters tool
*arguments* inside the same OS process as the controller; it is not a security
boundary against a malicious runtime process itself. The real boundary is the
sandbox container (`DockerSandbox`, network off, no credentials) that the tool's
file operations and the controller's own `SandboxPort.exec` calls execute inside.
Phase 3 moves the runtime process itself into that container so the guard becomes
defense in depth rather than the only line; see `factory-controller/README.md`.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from .. import globs

logger = logging.getLogger(__name__)

# Only these tools are ever allowed; everything else (Bash, WebFetch, WebSearch, ...)
# is denied without inspecting its arguments.
READ_ONLY_TOOLS = frozenset({"Read", "Glob", "Grep"})
WRITE_TOOLS = frozenset({"Edit", "Write"})
ALLOWED_TOOLS = READ_ONLY_TOOLS | WRITE_TOOLS

# Where each tool carries the path(s) it wants to touch.
_PATH_KEYS = ("file_path", "path", "notebook_path")


@dataclass(frozen=True)
class ToolGuard:
    """A per-task `on_tool_approval` callback (final draft §12.3)."""
    workdir: str
    owned_paths: tuple
    forbidden_globs: tuple    # floor.forbidden_paths + product.forbidden_paths
    protected_globs: tuple    # floor.protected_paths + product.protected_paths

    def _resolved_root(self) -> Path:
        return Path(self.workdir).resolve()

    def _extract_paths(self, tool_input: dict) -> list[str]:
        found = [tool_input[key] for key in _PATH_KEYS if isinstance(tool_input.get(key), str)]
        return found

    def _within_workdir(self, raw_path: str) -> Path | None:
        """Resolve `raw_path` (symlinks and `..` included) against the workdir.

        Returns the resolved `Path` if it stays inside the sandbox workdir, else None.
        """
        root = self._resolved_root()
        candidate = Path(raw_path)
        resolved = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            return None
        return resolved

    def _relative(self, resolved: Path) -> str:
        return resolved.relative_to(self._resolved_root()).as_posix()

    def __call__(self, tool_name: str, tool_input: dict) -> bool:
        if tool_name not in ALLOWED_TOOLS:
            logger.warning("tool guard: denying tool %r outright (not in the allowed set)", tool_name)
            return False

        paths = self._extract_paths(tool_input)
        if not paths:
            # Glob/Grep with no explicit path search the cwd, which is already the
            # sandbox workdir -- nothing to resolve, nothing to deny.
            return tool_name in READ_ONLY_TOOLS

        for raw in paths:
            resolved = self._within_workdir(raw)
            if resolved is None:
                logger.warning("tool guard: denying %s outside the sandbox workdir: %r", tool_name, raw)
                return False
            if tool_name in WRITE_TOOLS:
                rel = self._relative(resolved)
                if globs.match_any(rel, self.forbidden_globs):
                    logger.warning("tool guard: denying %s on forbidden path %r", tool_name, rel)
                    return False
                if globs.match_any(rel, self.protected_globs):
                    logger.warning("tool guard: denying %s on protected path %r", tool_name, rel)
                    return False
                if not globs.match_any(rel, self.owned_paths):
                    logger.warning("tool guard: denying %s outside owned_paths: %r", tool_name, rel)
                    return False
        return True


__all__ = ["ALLOWED_TOOLS", "READ_ONLY_TOOLS", "WRITE_TOOLS", "ToolGuard"]
