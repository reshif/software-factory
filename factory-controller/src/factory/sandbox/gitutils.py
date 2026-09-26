"""Shared git plumbing for turning a sandbox working copy into a `Diff`.

Both `DockerSandbox` and `LocalSandbox` stage every change the same way
(build spec §3 B3): `git add -A`, then `git diff --cached --binary <base>`
for the patch, plus `--numstat` for line counts and `--name-status` for file
statuses. Every call passes `--no-renames` so path parsing never depends on
the ambient git config: a rename always shows up as a delete of the old path
plus an add of the new one, which keeps parsing simple and deterministic.
"""
from __future__ import annotations

import re
import subprocess

from ..models import Diff
from ..policy.action_classes import FileChange

_STATUS_CODES = {"A": "added", "M": "modified", "D": "deleted"}
_DIFF_HEADER = re.compile(r"^diff --git a/(?P<a>.*) b/(?P<b>.*)$")


def run_git(workdir: str, args: list[str]) -> str:
    """Run a git command in `workdir` and return stdout. Raises on non-zero exit."""
    proc = subprocess.run(
        ["git", "-C", workdir, *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


def _parse_name_status(text: str) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        code, path = line.split("\t", 1)
        statuses[path] = _STATUS_CODES.get(code[0], "modified")
    return statuses


def _parse_numstat(text: str) -> dict[str, tuple[int, int]]:
    counts: dict[str, tuple[int, int]] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        added, removed, path = line.split("\t", 2)
        counts[path] = (0 if added == "-" else int(added), 0 if removed == "-" else int(removed))
    return counts


def _split_patch_by_file(patch: str) -> dict[str, str]:
    """Map each file's path (the `b/` side of `diff --git a/x b/y`) to its chunk of the patch."""
    chunks: dict[str, str] = {}
    current_path: str | None = None
    current_lines: list[str] = []
    for line in patch.splitlines(keepends=True):
        header = _DIFF_HEADER.match(line)
        if header:
            if current_path is not None:
                chunks[current_path] = "".join(current_lines)
            current_path = header.group("b")
            current_lines = [line]
        elif current_path is not None:
            current_lines.append(line)
    if current_path is not None:
        chunks[current_path] = "".join(current_lines)
    return chunks


def _added_text(chunk: str) -> str:
    """Concatenate a file's added lines (for weakening-pattern checks), skipping the `+++` header."""
    lines = [line[1:] for line in chunk.splitlines() if line.startswith("+") and not line.startswith("+++")]
    return "\n".join(lines)


def build_diff(workdir: str, base_commit: str) -> Diff:
    """Stage every change in `workdir` (including untracked files) and capture it as a `Diff`."""
    run_git(workdir, ["add", "-A"])
    name_status = _parse_name_status(
        run_git(workdir, ["diff", "--cached", "--no-renames", "--name-status", base_commit])
    )
    numstat = _parse_numstat(
        run_git(workdir, ["diff", "--cached", "--no-renames", "--numstat", base_commit])
    )
    patch = run_git(workdir, ["diff", "--cached", "--no-renames", "--binary", base_commit])
    chunks = _split_patch_by_file(patch)

    changes = []
    for path, status in name_status.items():
        added, removed = numstat.get(path, (0, 0))
        changes.append(
            FileChange(
                path=path,
                status=status,
                added_lines=added,
                removed_lines=removed,
                added_text=_added_text(chunks.get(path, "")),
            )
        )
    return Diff(base_commit=base_commit, changes=tuple(changes), patch=patch)
