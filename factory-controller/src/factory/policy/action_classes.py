"""Deterministic action-class classification of an actual diff (final draft §6.1, §9.2, §13).

The intake agent only *suggests* a class. The controller classifies the real diff
with this module, and the strictest of the two wins.
"""
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Iterable

from .. import globs
from .loader import Floor

CLASS_ORDER = ("AC1", "AC2", "AC3", "AC4", "AC5", "AC6", "AC7", "AC8")


def stricter_class(*classes: str) -> str:
    return max(classes, key=CLASS_ORDER.index)


@dataclass(frozen=True)
class FileChange:
    path: str
    status: str               # added | modified | deleted | renamed
    added_lines: int = 0
    removed_lines: int = 0
    added_text: str = ""      # concatenated added lines, for weakening-pattern checks

    def __post_init__(self):
        if self.status not in ("added", "modified", "deleted", "renamed"):
            raise ValueError(f"unknown change status {self.status!r} for {self.path}")


@dataclass(frozen=True)
class Classification:
    action_class: str
    reasons: tuple
    protected_touched: tuple
    forbidden_touched: tuple
    diff_lines: int

    @property
    def blocked(self) -> bool:
        return self.action_class == "AC8"


def classify(changes: Iterable[FileChange], floor: Floor, *,
             product_protected: Iterable[str] = (), product_forbidden: Iterable[str] = (),
             declared: str | None = None) -> Classification:
    changes = list(changes)
    if not changes:
        raise ValueError("cannot classify an empty diff")
    forbidden_globs = tuple(floor.forbidden_paths) + tuple(product_forbidden)
    protected_globs = tuple(floor.protected_paths) + tuple(product_protected)

    reasons: list[str] = []
    forbidden = [c.path for c in changes if globs.match_any(c.path, forbidden_globs)]
    weakened = [c.path for c in changes
                if any(p in c.added_text for p in floor.weakening_patterns)]
    protected = [c.path for c in changes if globs.match_any(c.path, protected_globs)]
    touched_existing_tests = [
        c.path for c in changes
        if floor.existing_tests_protected and c.status != "added"
        and globs.match_any(c.path, floor.test_globs)
    ]
    migrations = [c for c in changes if globs.match_any(c.path, floor.migration_paths)]
    diff_lines = sum(c.added_lines + c.removed_lines for c in changes)

    candidates = []
    if forbidden:
        candidates.append("AC8")
        reasons.append(f"forbidden paths: {forbidden}")
    if weakened:
        candidates.append("AC8")
        reasons.append(f"check-weakening patterns added in: {weakened}")
    if protected or touched_existing_tests:
        candidates.append("AC6")
        if protected:
            reasons.append(f"protected paths: {protected}")
        if touched_existing_tests:
            reasons.append(f"existing tests modified or deleted: {touched_existing_tests}")
    for m in migrations:
        if m.status == "added":
            candidates.append("AC5")
            reasons.append(f"additive migration: {m.path}")
        else:
            candidates.append("AC6")
            reasons.append(f"existing migration changed: {m.path}")

    candidates.append(_shape_class(changes, floor, diff_lines, reasons))
    if declared:
        candidates.append(declared)
        reasons.append(f"declared class {declared}")

    return Classification(
        action_class=stricter_class(*candidates),
        reasons=tuple(reasons),
        protected_touched=tuple(sorted(set(protected + touched_existing_tests))),
        forbidden_touched=tuple(sorted(set(forbidden + weakened))),
        diff_lines=diff_lines,
    )


def _shape_class(changes, floor: Floor, diff_lines: int, reasons: list) -> str:
    def is_doc(c):
        return globs.match_any(c.path, floor.docs_globs)

    def is_new_test(c):
        return c.status == "added" and globs.match_any(c.path, floor.test_globs)

    def is_dependency(c):
        name = PurePosixPath(c.path).name
        return any(globs.match(name, pattern) for pattern in floor.dependency_files)

    if all(is_doc(c) or is_new_test(c) for c in changes):
        reasons.append("docs and/or new tests only")
        return "AC1"
    if all(is_dependency(c) for c in changes):
        reasons.append("dependency manifests only")
        return "AC2"
    if diff_lines <= floor.small_fix_max_lines:
        reasons.append(f"small change: {diff_lines} lines <= {floor.small_fix_max_lines}")
        return "AC3"
    reasons.append(f"feature-sized change: {diff_lines} lines")
    return "AC4"
