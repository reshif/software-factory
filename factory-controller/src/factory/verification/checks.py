"""Fail-closed evaluation of required checks (final draft §9.2, §13).

"The evidence gate requires an actual success conclusion for every required check,
because GitHub treats skipped and neutral as passing." Only ``CheckResult.conclusion
== "success"`` counts. Anything else -- failure, skipped, neutral, cancelled,
timed_out, or simply absent -- fails the gate.
"""
from dataclasses import dataclass

from ..models import CheckResult

PASSING_CONCLUSION = "success"


@dataclass(frozen=True)
class CheckVerdict:
    """The outcome of evaluating a set of required checks against actual results."""
    ok: bool
    failing: tuple  # tuple[str, ...] -- required checks present but not "success"
    missing: tuple  # tuple[str, ...] -- required checks with no result at all


def evaluate(required: list[str], results: dict[str, CheckResult]) -> CheckVerdict:
    """Evaluate `required` check names against `results`. Fail closed.

    A required check is missing if it has no entry in `results`, and failing if its
    entry's conclusion isn't exactly "success". Both make the verdict not-ok.
    """
    missing = tuple(name for name in required if name not in results)
    failing = tuple(
        name for name in required
        if name in results and results[name].conclusion != PASSING_CONCLUSION
    )
    return CheckVerdict(ok=not missing and not failing, failing=failing, missing=missing)
