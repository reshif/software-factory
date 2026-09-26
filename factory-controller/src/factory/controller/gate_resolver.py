"""One gate table, one precedence rule: the strictest applicable rule wins (final draft §6.2).

Precedence:
  1. AC8 forbidden                -> blocked
  2. AC7 authority change         -> hx
  3. protected paths              -> combined with the AC6 row
  4. base table                   -> autonomy level L3
  5. autonomy relaxations         -> only the cells they name, never protected or never-relax classes
  6. product overrides            -> may only tighten
"""
from dataclasses import dataclass

from ..policy import Policy, PolicyError, Requirement, parse_requirement, strictest
from ..policy.loader import CLASSES, GATES, LEVELS, PROFILES


@dataclass(frozen=True)
class GateDecision:
    action_class: str
    risk_profile: str
    autonomy_level: str
    h1: Requirement
    hm: Requirement
    h2: Requirement
    rules: tuple            # every rule that shaped the result, for the evidence bundle

    def gate(self, name: str) -> Requirement:
        return getattr(self, name)


def resolve(policy: Policy, *, action_class: str, risk_profile: str, autonomy_level: str = "L3",
            protected_touched: bool = False, covered_by_standing: bool = False,
            overrides: dict | None = None) -> GateDecision:
    if action_class not in CLASSES:
        raise PolicyError(f"unknown action class {action_class!r}")
    if risk_profile not in PROFILES:
        raise PolicyError(f"unknown risk profile {risk_profile!r}")
    if autonomy_level not in LEVELS:
        raise PolicyError(f"unknown autonomy level {autonomy_level!r}")
    max_level = policy.max_level_by_profile[risk_profile]
    if LEVELS.index(autonomy_level) > LEVELS.index(max_level):
        raise PolicyError(f"{risk_profile} products are capped at {max_level}, got {autonomy_level}")

    rules = [f"policy {policy.policy_version}"]

    # 1–2: forbidden and authority changes short-circuit everything else.
    if action_class in ("AC8", "AC7"):
        row = policy.base[action_class]
        rules.append(f"base.{action_class}")
        cells = {g: row[g][risk_profile] for g in GATES}
        return GateDecision(action_class, risk_profile, autonomy_level, rules=tuple(rules), **cells)

    # 4: base table.
    cells = {g: policy.base[action_class][g][risk_profile] for g in GATES}
    rules.append(f"base.{action_class}")

    # 3: protected paths pull in the AC6 row (strictest per gate).
    if protected_touched and action_class != "AC6":
        cells = {g: strictest(cells[g], policy.base["AC6"][g][risk_profile]) for g in GATES}
        rules.append("protected_paths->AC6")

    # 5: autonomy relaxations, only for plain (unprotected, relaxable) cells.
    if not protected_touched and action_class not in policy.never_relax:
        for relax in policy.relaxations[autonomy_level]:
            if action_class in relax.classes and risk_profile in relax.profiles:
                cells[relax.gate] = relax.to
                rules.append(f"{autonomy_level}.{relax.gate}->{relax.to}")

    # H1 'standing' only holds when a standing mandate actually covers the work;
    # otherwise the work needs a mission approval like a feature.
    # (Protected work never reaches here as 'standing': the AC6 row has already made H1 stricter.)
    if cells["h1"].kind == "standing" and not covered_by_standing:
        cells["h1"] = policy.base["AC4"]["h1"][risk_profile]
        rules.append("not_covered->mission_h1")

    # 6: product overrides may only tighten.
    for gate, value in ((overrides or {}).get(action_class) or {}).items():
        if gate not in GATES:
            raise PolicyError(f"override names unknown gate {gate!r}")
        tightened = strictest(cells[gate], parse_requirement(value))
        if tightened != cells[gate]:
            rules.append(f"override.{action_class}.{gate}->{tightened}")
        cells[gate] = tightened

    return GateDecision(action_class, risk_profile, autonomy_level, rules=tuple(rules), **cells)


def release_requirement(decisions: list[GateDecision]) -> Requirement:
    """H2 for a digest is the strictest H2 among all missions it contains (final draft §6.2)."""
    if not decisions:
        raise ValueError("a release must contain at least one mission")
    return strictest(*(d.h2 for d in decisions))


def hx_requirement(policy: Policy, risk_profile: str, *, destructive_or_credentials: bool = False) -> Requirement:
    base = policy.hx_quorum[risk_profile]
    return strictest(base, Requirement("approve", approvals=2)) if destructive_or_credentials else base
