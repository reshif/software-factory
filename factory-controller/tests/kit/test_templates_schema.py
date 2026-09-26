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


def test_verification_required_includes_review_agent_and_holdout_blackbox(backend_service):
    """Final review item 1: the golden path must demonstrate independent
    verification, not just local checks. B7's ProductRegistry enforces
    review_agent for every product and holdout_blackbox for standard/
    regulated risk profiles as a floor; the template states both explicitly
    rather than relying on that enforced default."""
    profile = yaml.safe_load((backend_service / "factory.yaml").read_text())
    required = profile["verification"]["required"]
    assert "review_agent" in required
    assert "holdout_blackbox" in required
    assert required == ["lint", "unit", "review_agent", "holdout_blackbox"]


def test_product_registry_loads_the_backend_service_template(kit_dir):
    """Integration check: the exact loader B7's pipeline uses at startup
    (factory.pipeline.products.ProductRegistry) must accept this template
    without a ProductConfigError -- review_agent/holdout_blackbox need no
    `checks` command (NO_COMMAND_REQUIRED), only lint/unit do."""
    from factory.pipeline.products import ProductRegistry

    registry = ProductRegistry(kit_dir / "templates", kit_dir)
    product = registry.get("backend-service")
    assert product.verification_required == ("lint", "unit", "review_agent", "holdout_blackbox")
    assert set(product.checks) == {"lint", "unit"}


def test_backend_service_template_files_present(backend_service):
    for rel in (
        "factory.yaml", "CLAUDE.md", "AGENTS.md", "CODEOWNERS", ".gitignore",
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
        "CODEOWNERS", "README.md", "runner/run_blackbox.py", "runner/miniyaml.py",
        ".github/workflows/holdout.yml",
    ):
        assert (holdouts / rel).is_file(), f"missing {rel}"
    assert list((holdouts / "scenarios").glob("*.yaml")), "no scenario files"


def test_holdouts_repo_readme_documents_the_token_permission(kit_dir):
    """Final review item 4: the holdout token needs Actions: write on the
    holdouts repo (for workflow_dispatch) and read on artifacts."""
    text = (kit_dir / "templates" / "holdouts-repo" / "README.md").read_text()
    assert "Actions: write" in text
    assert "artifact" in text.lower()
    assert "correlation_id" in text


def test_holdout_workflow_takes_a_correlation_id_and_sets_run_name(kit_dir):
    """Q-M3: B5's verification runner polls this workflow_dispatch run by
    matching on an exact run-name, since the dispatch API hands back no run
    id. The line must be byte-exact -- not just "close enough" -- or that
    match fails."""
    text = (kit_dir / "templates" / "holdouts-repo" / ".github" / "workflows" / "holdout.yml").read_text()
    assert "run-name: holdout ${{ inputs.correlation_id }}" in text.splitlines()

    doc = yaml.safe_load(text)
    triggers = doc[True] if True in doc else doc["on"]  # PyYAML may parse bare `on:` as boolean True
    inputs = triggers["workflow_dispatch"]["inputs"]
    assert "correlation_id" in inputs
    assert inputs["correlation_id"]["required"] is True
    assert inputs["correlation_id"]["type"] == "string"
