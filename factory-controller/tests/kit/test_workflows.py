"""factory-kit/workflows/*.yml: reusable CI and deploy workflows (spec §3 B6,
final draft §13.2)."""
import yaml


def _load(kit_dir, name):
    return yaml.safe_load((kit_dir / "workflows" / name).read_text())


def _code_only(text: str) -> str:
    """Strip `#` comments so a check for real usage isn't fooled by prose
    explaining what NOT to do (both workflows document the banned patterns
    in their header comments)."""
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def test_ci_pr_has_no_secrets_and_read_only_permissions(kit_dir):
    doc = _load(kit_dir, "ci-pr.yml")
    assert doc["permissions"] == {"contents": "read"}
    assert "secrets" not in doc
    code = _code_only((kit_dir / "workflows" / "ci-pr.yml").read_text())
    assert "secrets." not in code


def test_ci_pr_is_workflow_call_and_pull_request_safe(kit_dir):
    doc = _load(kit_dir, "ci-pr.yml")
    triggers = doc[True] if True in doc else doc["on"]  # YAML parses bare `on:` as True in some loaders
    assert set(triggers) == {"pull_request", "workflow_call"}, "must trigger on pull_request, never pull_request_target"
    assert doc["jobs"]["verify"]["steps"]  # has real steps, not a stub


def test_deploy_is_environment_scoped_with_oidc(kit_dir):
    doc = _load(kit_dir, "deploy.yml")
    assert doc["permissions"]["id-token"] == "write"
    assert doc["permissions"]["contents"] == "read"
    triggers = doc[True] if True in doc else doc["on"]
    assert "workflow_call" in triggers
    assert "environment" in triggers["workflow_call"]["inputs"]

    job = doc["jobs"]["deploy"]
    assert job["environment"] == "${{ inputs.environment }}"


def test_deploy_carries_no_static_secrets(kit_dir):
    code = _code_only((kit_dir / "workflows" / "deploy.yml").read_text())
    assert "aws-secret-access-key" not in code.lower()
    assert "aws-access-key-id" not in code.lower()
    assert "secrets." not in code
