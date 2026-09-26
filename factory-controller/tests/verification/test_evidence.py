import json
from pathlib import Path

import jsonschema
import pytest

from factory.models import CheckResult, Diff
from factory.policy.action_classes import FileChange
from factory.policy.loader import default_kit_dir
from factory.verification.evidence import build_evidence

SCHEMA_PATH = default_kit_dir() / "schemas" / "evidence-bundle.schema.json"


@pytest.fixture(scope="module")
def schema():
    return json.loads(SCHEMA_PATH.read_text())


DIFF = Diff(base_commit="abc123", changes=(FileChange(path="a.py", status="modified", added_lines=3),),
            patch="diff --git a/a.py b/a.py\n+hello\n")


def test_build_evidence_writes_diff_and_sets_ref(tmp_path):
    bundle = build_evidence(
        mission_id="MIS-1", revision="rev-1", diff=DIFF,
        checks={"tests": CheckResult("tests", "success")}, action_class="AC3", rule_fired="patch-auto",
        evidence_dir=str(tmp_path))
    diff_path = Path(bundle.diff_ref)
    assert diff_path.exists()
    assert diff_path.read_text() == DIFF.patch
    assert bundle.content_hash == DIFF.content_hash


def test_to_dict_validates_against_schema(tmp_path, schema):
    bundle = build_evidence(
        mission_id="MIS-1", revision="rev-1", diff=DIFF,
        checks={"tests": CheckResult("tests", "success"), "lint": CheckResult("lint", "failure", detail="oops")},
        action_class="AC3", rule_fired="patch-auto", evidence_dir=str(tmp_path),
        task_ids=("T-1",), holdout=(9, 10), blast_radius=("service-a",),
        rollback_plan="revert the commit", rollback_tested=True, cost_usd=1.23, tokens=456,
        ci_minutes=2.5, untrusted_inputs=("issue#42",), agent_versions={"implementer": "1.0"})
    doc = bundle.to_dict()
    jsonschema.validate(doc, schema)


def test_to_dict_without_optional_fields_still_validates(tmp_path, schema):
    bundle = build_evidence(
        mission_id="MIS-1", revision="rev-1", diff=DIFF, checks={}, action_class="AC1",
        rule_fired="standing-mandate", evidence_dir=str(tmp_path))
    doc = bundle.to_dict()
    jsonschema.validate(doc, schema)
    assert "holdout" not in doc
    assert "rollback" not in doc


def test_diff_ref_is_idempotent_for_same_content(tmp_path):
    bundle1 = build_evidence(mission_id="MIS-1", revision="rev-1", diff=DIFF, checks={},
                              action_class="AC1", rule_fired="r", evidence_dir=str(tmp_path))
    bundle2 = build_evidence(mission_id="MIS-1", revision="rev-2", diff=DIFF, checks={},
                              action_class="AC1", rule_fired="r", evidence_dir=str(tmp_path))
    assert bundle1.diff_ref == bundle2.diff_ref
