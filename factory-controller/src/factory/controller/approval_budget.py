"""Approval budget: reviewer capacity is the WIP limit (final draft §6.5)."""

# Estimated reviewer minutes per unit at L3, standard profile. Calibrate from Phase 0/2 data.
MINUTES_PER_UNIT = {"feature": 65, "patch": 10, "hx": 10}


def weekly_minutes(reviewer_hours_per_week: float) -> float:
    return reviewer_hours_per_week * 60


def can_admit(lane: str, *, committed_minutes: float, reviewer_hours_per_week: float) -> bool:
    """Admit a new mission only if its projected approval minutes still fit this week's budget."""
    if lane not in MINUTES_PER_UNIT:
        raise ValueError(f"unknown lane {lane!r}")
    return committed_minutes + MINUTES_PER_UNIT[lane] <= weekly_minutes(reviewer_hours_per_week)
