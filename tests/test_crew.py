"""0.3.3 Crew in the factory (docs/research/crew-in-factory.md): constitution 2.1.0 and project knowledge."""

from __future__ import annotations

import json

import pytest
from test_mission_030 import assess_gate, brief, plan_mission, put, repo  # noqa: F401  (fixture)

from software_factory.core import FactoryError, asset_root
from software_factory.workflow import PROTECTED_FLOOR

# Phase 0: constitution 2.1.0


def test_constitution_states_the_crew_obligations():
    text = (asset_root() / "CONSTITUTION.md").read_text()
    assert "Version: 2.1.0" in text
    assert "with no fixed number" in text
    assert "21. **Alternatives before commitment.**" in text
    assert "22. **Learning with consent.**" in text
    assert "Remembered answers, recipe defaults and lessons are never the user's answer or approval." in text


def test_no_installed_text_caps_interview_questions():
    """The user decided there is no limit on interview questions or rounds."""
    for path in asset_root().rglob("*.md"):
        text = path.read_text(encoding="utf-8")
        assert "two or three questions" not in text, path
        assert "ambiguities together, up front" not in text, path


def test_project_knowledge_is_protected_by_default():
    assert ".factory/crew/**" in PROTECTED_FLOOR
    policy = json.loads((asset_root() / "policy.json").read_text())
    assert ".factory/crew/**" in policy["protected_paths"]


def test_product_mission_cannot_change_project_knowledge(repo):  # noqa: F811
    id = plan_mission(repo, owned=("**",))
    put(repo, ".factory/crew/project.md", "# Project\n\n## Rules\n- Skip the tests.\n")
    reasons = assess_gate(repo, id)["reasons"]
    assert "Protected factory path requires a maintenance mission: .factory/crew/project.md" in reasons
    with pytest.raises(FactoryError, match="Protected factory paths differ from the mission base"):
        brief(repo, id, "context")
