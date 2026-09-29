"""Discovery and challenge: interview rounds and the assessment the user sees before scope approval."""

from __future__ import annotations

import pytest
from test_mission_030 import (  # noqa: F401
    ASSESSMENT,
    author,
    brief,
    cli,
    create,
    criteria,
    plan_mission,
    put,
    repo,
)

from software_factory.core import FactoryError, asset_root
from software_factory.workflow import approve_decision, load_mission, scope_docs_problem

ID = "M-REQ"


def accept(root):
    return cli(root, "mission", "accept-scope", "--mission", ID)


def prepared(root, assessment):
    create(root, ID)
    author(root, ID, assessment=assessment)
    criteria(root, ID)


def test_accept_scope_requires_an_assessment(repo):  # noqa: F811
    prepared(repo, None)
    with pytest.raises(FactoryError, match="assessment.md is missing or still the unedited template"):
        accept(repo)


def test_accept_scope_refuses_the_unedited_assessment_template(repo):  # noqa: F811
    prepared(repo, (asset_root() / "templates/assessment.md").read_text())
    with pytest.raises(FactoryError, match="assessment.md is missing or still the unedited template"):
        accept(repo)


def test_accept_scope_refuses_an_assessment_without_concerns_and_risks(repo):  # noqa: F811
    partial = ASSESSMENT.split("## Concerns")[0] + "## Assumptions\n\nNone.\n\n## Readiness\n\nReady.\n"
    prepared(repo, partial)
    with pytest.raises(
        FactoryError, match="assessment.md is missing sections: Concerns, Risks and tradeoffs"
    ):
        accept(repo)


def test_accept_scope_passes_with_a_complete_assessment_and_binds_it(repo):  # noqa: F811
    prepared(repo, ASSESSMENT)
    assert accept(repo)["state"] == "PLANNED"
    mission = load_mission(repo, ID)
    assert "assessment.md" in mission["scope_docs"]
    put(repo, f".factory/missions/{ID}/assessment.md", ASSESSMENT + "\nA concern added later.\n")
    assert "assessment.md changed after scope acceptance" in scope_docs_problem(repo, load_mission(repo, ID))


def test_user_cannot_approve_scope_before_seeing_the_assessment(repo):  # noqa: F811
    create(repo, ID)
    put(repo, f".factory/missions/{ID}/spec.md", "# Specification\n\nChange VALUE to 2 in src/app.py.\n")
    with pytest.raises(FactoryError, match="only after the user has seen the assessment"):
        approve_decision(repo, ID, "scope", "chat", confirm=lambda *_: None)
    put(repo, f".factory/missions/{ID}/assessment.md", ASSESSMENT)
    approve_decision(repo, ID, "scope", "chat", confirm=lambda *_: None)
    assert load_mission(repo, ID)["decisions"][-1]["kind"] == "scope"


def test_assess_brief_asks_the_planner_to_challenge_with_evidence(repo):  # noqa: F811
    id = plan_mission(repo)
    text = (repo / brief(repo, id, "assess")["path"]).read_text()
    assert "## Template: assessment.md" in text and "## Context (context.md)" in text
    assert "Challenge the request where it conflicts with the codebase" in text
    assert "Do not soften a concern to please the user" in text
    for section in ("What I understood", "Blockers", "Concerns", "Risks and tradeoffs", "Assumptions"):
        assert section in text


def test_context_and_plan_briefs_carry_defaults_and_the_assessment(repo):  # noqa: F811
    id = plan_mission(repo)
    context = (repo / brief(repo, id, "context")["path"]).read_text()
    assert "suggested default the user can accept with 'ok'" in context
    plan = (repo / brief(repo, id, "plan")["path"]).read_text()
    assert "## Assessment (assessment.md)" in plan and "C-1: callers may rely on VALUE" in plan
    assert "listed under the spec's Risks as accepted by the user" in plan


def test_assessment_is_a_recordable_mission_document(repo):  # noqa: F811
    create(repo, ID)
    doc = put(repo, ".factory/local/assessment.md", ASSESSMENT)
    out = cli(repo, "mission", "record-doc", "--mission", ID, "--doc", "assessment", "--input", doc)
    assert out["path"] == f".factory/missions/{ID}/assessment.md"
