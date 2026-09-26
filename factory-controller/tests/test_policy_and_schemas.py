import json
import shutil
from pathlib import Path

import jsonschema
import pytest
import yaml

from factory.policy import PolicyError, load_policy
from factory.policy.loader import CLASSES, GATES, PROFILES, default_kit_dir

KIT = default_kit_dir()

EXAMPLES = {
    "factory.yaml": "factory.schema.json",
    "standing-mandate.yaml": "mandate.schema.json",
    "mission-mandate.yaml": "mandate.schema.json",
    "task-contract.yaml": "task-contract.schema.json",
    "approval-record.yaml": "approval-record.schema.json",
    "evidence-bundle.yaml": "evidence-bundle.schema.json",
}


def test_policy_loads_complete_table(policy):
    assert policy.policy_version == "p-2026.10.3"
    for cls in CLASSES:
        for gate in GATES:
            assert set(policy.base[cls][gate]) == set(PROFILES)


def test_never_relax_classes_cannot_be_relaxed(policy):
    for level, relaxations in policy.relaxations.items():
        for r in relaxations:
            assert not (r.classes & policy.never_relax), level


def test_l5_inherits_l4(policy):
    assert set(policy.relaxations["L4"]) <= set(policy.relaxations["L5"])


def test_policy_version_mismatch_is_rejected(tmp_path):
    shutil.copytree(KIT / "policies", tmp_path / "policies")
    floor = tmp_path / "policies" / "floor.yaml"
    floor.write_text(floor.read_text().replace("p-2026.10.3", "p-other"))
    with pytest.raises(PolicyError, match="policy_version mismatch"):
        load_policy(tmp_path)


def test_relaxing_a_protected_class_is_rejected(tmp_path):
    shutil.copytree(KIT / "policies", tmp_path / "policies")
    levels = tmp_path / "policies" / "autonomy-levels.yaml"
    levels.write_text(levels.read_text().replace("classes: [AC1, AC2, AC3, AC4]", "classes: [AC1, AC6]"))
    with pytest.raises(PolicyError, match="never-relax"):
        load_policy(tmp_path)


@pytest.mark.parametrize("example,schema", EXAMPLES.items())
def test_examples_match_schemas(example, schema):
    doc = yaml.safe_load((KIT / "examples" / example).read_text())
    schema_doc = json.loads((KIT / "schemas" / schema).read_text())
    jsonschema.Draft202012Validator.check_schema(schema_doc)
    validator = jsonschema.Draft202012Validator(schema_doc, format_checker=jsonschema.FormatChecker())
    errors = sorted(validator.iter_errors(doc), key=lambda e: e.path)
    assert not errors, [f"{list(e.path)}: {e.message}" for e in errors]


def test_schema_rejects_unknown_risk_profile():
    schema_doc = json.loads((KIT / "schemas" / "factory.schema.json").read_text())
    doc = yaml.safe_load((KIT / "examples" / "factory.yaml").read_text())
    doc["risk_profile"] = "yolo"
    assert list(jsonschema.Draft202012Validator(schema_doc).iter_errors(doc))


def test_every_schema_pins_schema_version():
    for path in Path(KIT / "schemas").glob("*.json"):
        schema_doc = json.loads(path.read_text())
        assert schema_doc["properties"]["schema_version"] == {"const": 1}, path.name
        assert "schema_version" in schema_doc["required"], path.name
