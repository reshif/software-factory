"""Deterministic standing-mandate coverage (final draft §6.1).

Checked twice: before work starts (labels + suggested class) and on the actual diff.
A model never decides coverage.
"""
from dataclasses import dataclass, field
from datetime import date

from .. import globs
from ..policy.action_classes import Classification, FileChange


@dataclass(frozen=True)
class StandingMandate:
    mandate_id: str
    action_classes: frozenset
    paths: tuple
    max_diff_lines: int
    expires: date
    labels_any: tuple = ()
    exclude_paths: tuple = ()

    @classmethod
    def from_dict(cls, doc: dict, *, product_protected=(), product_forbidden=()) -> "StandingMandate":
        if doc.get("kind") != "standing":
            raise ValueError(f"{doc.get('mandate_id')}: not a standing mandate")
        cov = doc["coverage"]
        exclude = tuple(product_protected) + tuple(product_forbidden) if cov.get("exclude_paths_from") else ()
        return cls(mandate_id=doc["mandate_id"], action_classes=frozenset(cov["action_classes"]),
                   paths=tuple(cov["paths"]), max_diff_lines=int(cov["max_diff_lines"]),
                   expires=date.fromisoformat(str(doc["expires"])),
                   labels_any=tuple(cov.get("labels_any", ())), exclude_paths=exclude)


@dataclass(frozen=True)
class CoverageResult:
    covered: bool
    reasons: tuple = field(default=())


def check_request(mandate: StandingMandate, *, labels, suggested_class: str, today: date) -> CoverageResult:
    """Pre-run check: may this work start under the standing mandate?"""
    reasons = []
    if today > mandate.expires:
        reasons.append(f"mandate expired on {mandate.expires}")
    if mandate.labels_any and not set(labels) & set(mandate.labels_any):
        reasons.append(f"none of the required labels {list(mandate.labels_any)}")
    if suggested_class not in mandate.action_classes:
        reasons.append(f"class {suggested_class} not in {sorted(mandate.action_classes)}")
    return CoverageResult(not reasons, tuple(reasons))


def check_diff(mandate: StandingMandate, *, classification: Classification, changes: list[FileChange],
               today: date) -> CoverageResult:
    """Post-run check on the diff the controller captured. Failing it routes the work to H1."""
    reasons = []
    if today > mandate.expires:
        reasons.append(f"mandate expired on {mandate.expires}")
    if classification.action_class not in mandate.action_classes:
        reasons.append(f"diff classified {classification.action_class}, "
                       f"mandate allows {sorted(mandate.action_classes)}")
    if classification.diff_lines > mandate.max_diff_lines:
        reasons.append(f"diff has {classification.diff_lines} lines > {mandate.max_diff_lines}")
    outside = [c.path for c in changes if not globs.match_any(c.path, mandate.paths)]
    if outside:
        reasons.append(f"paths outside mandate: {outside}")
    excluded = [c.path for c in changes if globs.match_any(c.path, mandate.exclude_paths)]
    if excluded:
        reasons.append(f"excluded (protected/forbidden) paths: {excluded}")
    return CoverageResult(not reasons, tuple(reasons))
