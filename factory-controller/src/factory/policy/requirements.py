"""Gate requirements and the strictest-wins ordering (final draft §6.2)."""
from dataclasses import dataclass

_KIND_RANK = {"auto": 0, "standing": 1, "approve": 2, "hx": 3, "blocked": 4}


@dataclass(frozen=True)
class Requirement:
    kind: str                # auto | standing | approve | hx | blocked
    approvals: int = 0       # number of distinct human approvals (kind == "approve")
    security: bool = False   # one approver must hold the security role
    sampled: bool = False    # auto with a sampled audit (autonomy L4)

    def __post_init__(self):
        if self.kind not in _KIND_RANK:
            raise ValueError(f"unknown requirement kind: {self.kind!r}")
        if self.kind == "approve" and self.approvals < 1:
            raise ValueError("approve requirement needs at least one approval")

    @property
    def rank(self) -> tuple:
        return (_KIND_RANK[self.kind], self.approvals, self.security, self.sampled)

    @property
    def needs_human(self) -> bool:
        return self.kind in ("approve", "hx")

    def __str__(self) -> str:
        if self.kind == "approve":
            return f"{self.approvals}+sec" if self.security else str(self.approvals)
        if self.kind == "auto" and self.sampled:
            return "auto_sampled"
        return self.kind


def parse_requirement(value) -> Requirement:
    text = str(value).strip()
    if text in ("auto", "standing", "hx", "blocked"):
        return Requirement(text)
    if text == "auto_sampled":
        return Requirement("auto", sampled=True)
    security = text.endswith("+sec")
    number = text[:-4] if security else text
    if not number.isdigit():
        raise ValueError(f"cannot parse gate requirement {value!r}")
    return Requirement("approve", approvals=int(number), security=security)


def strictest(*requirements: Requirement) -> Requirement:
    """Combine requirements; the strictest wins.

    Approval counts and the security flag are merged, so "2" + "1+sec" gives "2+sec".
    """
    if not requirements:
        raise ValueError("strictest() needs at least one requirement")
    top = max(requirements, key=lambda r: _KIND_RANK[r.kind])
    if top.kind != "approve":
        return max((r for r in requirements if r.kind == top.kind), key=lambda r: r.rank)
    approvals = [r for r in requirements if r.kind == "approve"]
    return Requirement("approve",
                       approvals=max(r.approvals for r in approvals),
                       security=any(r.security for r in approvals))
