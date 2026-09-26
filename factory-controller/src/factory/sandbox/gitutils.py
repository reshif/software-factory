"""Turn a sandbox checkout into a `Diff` (final draft §12.1).

Every git call goes through `gitcmd.run_git` with an explicit `git_dir`
OUTSIDE the checkout it's diffing (see `sandbox/base.py`), and uses `-z`
(NUL-delimited, unquoted) output throughout: git's default `--name-status`/
`--numstat` C-quote any path with a space, a quote or a non-ASCII byte (e.g.
`".github/workflows/\\303\\251vil.yml"`), and a naive string match against
that quoted, escaped form would miss a real `.github/**` policy match —
letting an AC8 change through undetected (red team R-A2 / code review Q-H1).
`-z` output is never quoted, so the path we hand to the policy classifier is
always exactly the path git changed.

Per-file `added_text` comes from a separate `git diff -- <path>` call scoped
to that one path, not from regex-splitting the combined patch on
`diff --git a/... b/...` headers: a file's own added content can itself
contain a line that looks exactly like another file's diff header, which
would misattribute added lines under the old split-by-regex approach.
"""
from __future__ import annotations

from .. import gitcmd
from ..models import Diff
from ..policy.action_classes import FileChange

_STATUS_CODES = {"A": "added", "M": "modified", "D": "deleted"}


def _run(git_dir: str, work_tree: str, args: list[str]) -> str:
    return gitcmd.run_git(args, cwd=work_tree, git_dir=git_dir, work_tree=work_tree)


def _parse_name_status_z(output: str) -> dict[str, str]:
    """`--name-status -z --no-renames`: NUL-separated `(code, path)` pairs, verbatim bytes."""
    fields = output.split("\x00")
    statuses: dict[str, str] = {}
    i = 0
    while i + 1 < len(fields):
        code, path = fields[i], fields[i + 1]
        if code:
            statuses[path] = _STATUS_CODES.get(code[0], "modified")
        i += 2
    return statuses


def _parse_numstat_z(output: str) -> dict[str, tuple[int, int]]:
    """`--numstat -z --no-renames`: one NUL-terminated `<added>\\t<removed>\\t<path>` record per file."""
    counts: dict[str, tuple[int, int]] = {}
    for record in output.split("\x00"):
        if not record:
            continue
        added, removed, path = record.split("\t", 2)
        counts[path] = (0 if added == "-" else int(added), 0 if removed == "-" else int(removed))
    return counts


def _added_text(git_dir: str, work_tree: str, path: str) -> str:
    """A single file's added lines, scoped to it with a pathspec rather than parsed out of a bigger patch."""
    patch = _run(git_dir, work_tree, ["diff", "--cached", "--no-renames", "--", path])
    return "\n".join(line[1:] for line in patch.splitlines() if line.startswith("+") and not line.startswith("+++"))


def build_diff(git_dir: str, work_tree: str, base_commit: str) -> Diff:
    """Stage every change in `work_tree` (including untracked files) and capture it as a `Diff`."""
    _run(git_dir, work_tree, ["add", "-A"])
    name_status = _parse_name_status_z(
        _run(git_dir, work_tree, ["diff", "--cached", "--no-renames", "--name-status", "-z", base_commit])
    )
    numstat = _parse_numstat_z(
        _run(git_dir, work_tree, ["diff", "--cached", "--no-renames", "--numstat", "-z", base_commit])
    )
    patch = _run(git_dir, work_tree, ["diff", "--cached", "--no-renames", "--binary", base_commit])

    changes = []
    for path, status in name_status.items():
        added, removed = numstat.get(path, (0, 0))
        added_text = "" if status == "deleted" else _added_text(git_dir, work_tree, path)
        changes.append(
            FileChange(path=path, status=status, added_lines=added, removed_lines=removed, added_text=added_text)
        )
    return Diff(base_commit=base_commit, changes=tuple(changes), patch=patch)
