"""Alternatives before commitment: options graded against the criteria before scope approval.

For the feature lane (every kind but patch) the planner records ``options.md`` with two or
more genuinely different approaches ``O-<n>`` after the specification and criteria exist, and
a reviewer in a separate context records ``grading.md``: a score from 0 to 5 for every option
against every criterion, the winner and its weaknesses. The grading is bound to the exact
options, criteria and grade brief it judged. ``plan.md`` names the chosen option; choosing
another than the winner needs the user's recorded clarification. Grading is advice
(*Advice is not verification*): it informs the user's scope approval and satisfies no check.

``factory.json`` ``"crew": {"options": "off"}`` turns the requirement off, except for requests
from a contributor or an anonymous author.
"""

from __future__ import annotations

import re

from .core import FactoryError, digest, safe_path, sha256

OPTIONS_DOC = "options.md"
GRADING_DOC = "grading.md"
OPTION = re.compile(r"^###\s+(O-[0-9]{1,3})\b", re.MULTILINE)
FIELD = re.compile(
    r"^(Author|Options-sha256|Criteria-hash|Brief-sha256|Grader|Winner):[ \t]*(.*?)[ \t]*$", re.MULTILINE
)
DICTATED = re.compile(r'^Dictated by:\s*"(.+)"\s*$', re.MULTILINE)
CHOSEN = re.compile(
    r"^Chosen option:\s*(O-[0-9]{1,3})(?:\s*\(user override, clarification ([0-9]+)\))?\s*$", re.MULTILINE
)
PLACEHOLDER = re.compile(r"^<.*>$")


def required(mission: dict, config: dict) -> bool:
    if mission["kind"] == "patch":
        return False
    from .workflow import untrusted_source

    return (config.get("crew") or {}).get("options", "feature") != "off" or bool(untrusted_source(mission))


def _text(root, mission, name) -> str | None:
    target = safe_path(root, f".factory/missions/{mission['id']}/{name}")
    return target.read_text(encoding="utf-8", errors="replace") if target.is_file() else None


def _fields(text: str) -> dict:
    found = {}
    for key, value in FIELD.findall(text):
        if value and not PLACEHOLDER.fullmatch(value):
            found.setdefault(key, value)
    return found


def parse_scores(text: str) -> tuple[list[str], dict]:
    """The criteria columns and {option: {criterion: score}} of the grading table."""
    header, scores = [], {}
    for line in text.splitlines():
        cells = (
            [c.strip() for c in line.strip().strip("|").split("|")] if line.strip().startswith("|") else []
        )
        if not cells:
            continue
        if cells[0].lower() == "option":
            header = cells[1:]
        elif re.fullmatch(r"O-[0-9]{1,3}", cells[0]) and header:
            row = {}
            for criterion, cell in zip(header, cells[1:], strict=False):
                if re.fullmatch(r"[0-5]", cell):
                    row[criterion] = int(cell)
            scores[cells[0]] = row
    return header, scores


def option_reasons(root, mission: dict, texts: list[str] | None) -> tuple[list[str], list[str], str | None]:
    """(reasons, option ids, author) for options.md."""
    from .workflow import _normalized, mission_template

    text = _text(root, mission, OPTIONS_DOC)
    if text is None or text == mission_template(root, OPTIONS_DOC, mission["id"]):
        return (
            [
                "options.md is missing or still the template; brief the planner with `mission brief --kind options`"
            ],
            [],
            None,
        )
    reasons = []
    ids = OPTION.findall(text)
    if len(set(ids)) != len(ids):
        reasons.append("options.md repeats an option id")
    author = _fields(text).get("Author")
    if not author:
        reasons.append("options.md needs an `Author:` line naming the planner session that wrote it")
    if len(ids) < 2:
        dictated = DICTATED.search(text)
        if len(ids) == 1 and dictated:
            words = _normalized(dictated.group(1))
            if texts is None or len(words) < 8 or not any(words in _normalized(t) for t in texts):
                reasons.append(
                    "options.md has one option, but its `Dictated by:` quote matches nothing in the request or "
                    "clarifications (at least 8 characters)"
                )
        else:
            reasons.append(
                "options.md needs at least two genuinely different options (### O-1, ### O-2), or one option with a"
                ' `Dictated by: "<exact request words>"` line'
            )
    return reasons, sorted(set(ids)), author


def grade_brief_hash(root, mission: dict) -> str:
    from .workflow import render_brief

    return sha256(render_brief(root, mission, "grade"))


def reasons(root, mission: dict, config: dict, texts: list[str] | None = None) -> list[str]:
    """Why the options and their grading cannot yet support scope approval (empty when they can)."""
    if not required(mission, config):
        return []
    criteria = mission.get("criteria")
    if not criteria or not criteria.get("items"):
        return [
            "Options are graded against the criteria: record the criteria first (`mission brief --kind spec`)"
        ]
    found, ids, author = option_reasons(root, mission, texts)
    if found:
        return found
    grading = _text(root, mission, GRADING_DOC)
    from .workflow import mission_template

    if grading is None or grading == mission_template(root, GRADING_DOC, mission["id"]):
        return [
            "grading.md is missing; brief a reviewer with `mission brief --kind grade` and record its grading"
        ]
    problems = []
    fields = _fields(grading)
    options_hash = sha256((safe_path(root, f".factory/missions/{mission['id']}/{OPTIONS_DOC}")).read_bytes())
    if fields.get("Options-sha256") != options_hash:
        problems.append(
            "grading.md judged other options (Options-sha256 differs from options.md); grade again"
        )
    if fields.get("Criteria-hash") != digest(criteria):
        problems.append("grading.md judged other criteria (Criteria-hash differs); grade again")
    if fields.get("Brief-sha256") != grade_brief_hash(root, mission):
        problems.append("grading.md does not name the current grade brief (Brief-sha256); grade again")
    grader = fields.get("Grader")
    maintainer = (config.get("owners") or {}).get("maintainer")
    if not grader:
        problems.append("grading.md needs a `Grader:` line naming the reviewer session")
    elif grader == author or (maintainer and grader == maintainer):
        problems.append(
            "grading.md was graded by the options' author or the maintainer; it needs a separate reviewer"
        )
    winner = fields.get("Winner")
    if winner not in ids:
        problems.append(f"grading.md names winner {winner or 'none'}, which is not an option in options.md")
    _, scores = parse_scores(grading)
    wanted = [item["id"] for item in criteria["items"]]
    for option in ids:
        missing = [c for c in wanted if c not in scores.get(option, {})]
        if missing:
            problems.append(f"grading.md does not score {option} against {', '.join(missing)} (0 to 5)")
    plan = _text(root, mission, "plan.md") or ""
    chosen = CHOSEN.search(plan)
    if not chosen:
        problems.append("plan.md needs a line `Chosen option: O-<n>` naming the option it builds")
    elif chosen.group(1) not in ids:
        problems.append(f"plan.md chooses {chosen.group(1)}, which is not an option in options.md")
    elif chosen.group(1) != winner:
        clarification = chosen.group(2)
        count = len((mission.get("request") or {}).get("clarifications", []))
        if not clarification or not 1 <= int(clarification) <= count:
            problems.append(
                f"plan.md chooses {chosen.group(1)} over the grader's winner {winner}; only the user can, so cite "
                "their recorded clarification: `Chosen option: O-<n> (user override, clarification <N>)`"
            )
    return problems


def summary(root, mission: dict) -> dict | None:
    """Read-only view for status and the Deck."""
    grading = _text(root, mission, GRADING_DOC)
    options = _text(root, mission, OPTIONS_DOC)
    if options is None and grading is None:
        return None
    fields = _fields(grading or "")
    _, scores = parse_scores(grading or "")
    return {"options": sorted(set(OPTION.findall(options or ""))), "winner": fields.get("Winner"),
            "grader": fields.get("Grader"), "scores": scores}  # fmt: skip


def require_criteria(mission: dict) -> None:
    if not (mission.get("criteria") or {}).get("items"):
        raise FactoryError("Record the criteria first: options are written and graded against them")
