"""templates/backend-service: factory.yaml and mandates/SM-patch.yaml validate
against the kit schemas (spec §3 B6)."""
import json

import jsonschema
import pytest
import yaml


@pytest.fixture(scope="module")
def backend_service(kit_dir):
    return kit_dir / "templates" / "backend-service"


def _validate(doc, schema_path):
    schema_doc = json.loads(schema_path.read_text())
    jsonschema.Draft202012Validator.check_schema(schema_doc)
    validator = jsonschema.Draft202012Validator(schema_doc, format_checker=jsonschema.FormatChecker())
    errors = sorted(validator.iter_errors(doc), key=lambda e: e.path)
    assert not errors, [f"{list(e.path)}: {e.message}" for e in errors]


def test_backend_service_factory_yaml_matches_schema(kit_dir, backend_service):
    doc = yaml.safe_load((backend_service / "factory.yaml").read_text())
    _validate(doc, kit_dir / "schemas" / "factory.schema.json")


def test_backend_service_standing_mandate_matches_schema(kit_dir, backend_service):
    doc = yaml.safe_load((backend_service / "mandates" / "SM-patch.yaml").read_text())
    _validate(doc, kit_dir / "schemas" / "mandate.schema.json")


def test_factory_yaml_standing_mandates_reference_exists(backend_service):
    profile = yaml.safe_load((backend_service / "factory.yaml").read_text())
    mandate = yaml.safe_load((backend_service / "mandates" / "SM-patch.yaml").read_text())
    assert mandate["mandate_id"] in profile["standing_mandates"]
    assert mandate["product"] == profile["product"]


def test_factory_yaml_kit_version_matches_mandate(backend_service):
    profile = yaml.safe_load((backend_service / "factory.yaml").read_text())
    mandate = yaml.safe_load((backend_service / "mandates" / "SM-patch.yaml").read_text())
    assert profile["kit"]["version"] == mandate["kit_version"]
    assert profile["kit"]["policy_version"] == mandate["policy_version"]


def test_backend_service_template_files_present(backend_service):
    for rel in (
        "factory.yaml", "CLAUDE.md", "AGENTS.md", "CODEOWNERS",
        ".claude/settings.json", ".github/workflows/ci.yml",
        "mandates/SM-patch.yaml", "specs/README.md",
        "app/__init__.py", "app/store.py", "app/server.py",
        "tests/test_store.py", "tests/test_server.py",
    ):
        assert (backend_service / rel).is_file(), f"missing {rel}"


def test_claude_settings_json_is_valid_json(backend_service):
    settings = json.loads((backend_service / ".claude" / "settings.json").read_text())
    assert "factory-core@factory-kit" in settings["enabledPlugins"]


def test_holdouts_repo_template_files_present(kit_dir):
    holdouts = kit_dir / "templates" / "holdouts-repo"
    for rel in (
        "CODEOWNERS", "runner/run_blackbox.py", "runner/miniyaml.py",
        ".github/workflows/holdout.yml",
    ):
        assert (holdouts / rel).is_file(), f"missing {rel}"
    assert list((holdouts / "scenarios").glob("*.yaml")), "no scenario files"
