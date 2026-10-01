import json

import pytest

from software_factory.cli import build_parser
from software_factory.core import CONSTITUTION_PATH, FactoryError, sha256, write_json
from software_factory.installation import kernel_payload, starter
from software_factory.workflow import load_result


@pytest.mark.parametrize("command", ["migrate", "state"])
def test_removed_commands_are_not_exposed(command):
    with pytest.raises(SystemExit) as error:
        build_parser().parse_args([command])
    assert error.value.code == 2


def test_payload_contains_only_native_factory_runtime():
    paths = kernel_payload()
    assert not any(p.endswith(("migration.py", "legacy-assets.json", ".mjs", ".js")) for p in paths)
    assert not any(p.startswith((".factory/tools/", ".factory/tests/")) for p in paths)
    config = starter("product", "codex")
    assert config["jev"]["enabled"] is False
    assert config["jev"]["claim_mode"] == "shadow"
    assert "semantic_assistance" not in config
    assert "operations" not in config["jev"]


def test_constitution_has_one_canonical_location(tmp_path):
    from software_factory.installation import install

    # Besides the pinned runtime's own package data, the constitution has exactly one location.
    payload = kernel_payload()
    copies = [p for p in payload if p.endswith("CONSTITUTION.md") and not p.startswith(".factory/src/")]
    assert copies == [CONSTITUTION_PATH]
    (tmp_path / "CONSTITUTION.md").write_text("product-owned document")
    changed = install(tmp_path, selected="codex", skip_sync=True)["changed"]
    assert CONSTITUTION_PATH in changed and "CONSTITUTION.md" not in changed
    assert (tmp_path / "CONSTITUTION.md").read_text() == "product-owned document"
    assert (tmp_path / CONSTITUTION_PATH).read_bytes() == payload[CONSTITUTION_PATH]


def test_unindexed_flat_result_never_supplies_evidence(tmp_path):
    write_json(tmp_path, ".factory/missions/M-ONE/results/T-ONE.json", {"execution_attempt": 1})
    with pytest.raises(FactoryError, match="no indexed result"):
        load_result(tmp_path, "M-ONE", "T-ONE")


def test_index_requires_stored_attempt_and_matching_bytes(tmp_path):
    relative = ".factory/missions/M-ONE/results/records/T-ONE-record.json"
    write_json(tmp_path, relative, {"summary": "missing attempt"})
    index = {
        "schema_version": 1,
        "records": {
            "T-ONE": {"file": "T-ONE-record.json", "sha256": sha256((tmp_path / relative).read_bytes())}
        },
    }
    write_json(tmp_path, ".factory/missions/M-ONE/results/index.json", index)
    with pytest.raises(FactoryError, match="execution attempt"):
        load_result(tmp_path, "M-ONE", "T-ONE")
    (tmp_path / relative).write_text(json.dumps({"execution_attempt": 1}))
    with pytest.raises(FactoryError, match="hash mismatch"):
        load_result(tmp_path, "M-ONE", "T-ONE")


def test_removed_configuration_key_is_not_an_alias(tmp_path):
    from software_factory.core import validate

    config = starter("product", "codex")
    config["semantic_assistance"] = config.pop("jev")
    with pytest.raises(FactoryError, match="Invalid factory"):
        validate(tmp_path, "factory", config)


def test_indexed_non_object_is_a_controlled_error(tmp_path):
    relative = ".factory/missions/M-ONE/results/records/T-ONE-record.json"
    write_json(tmp_path, relative, [])
    write_json(
        tmp_path,
        ".factory/missions/M-ONE/results/index.json",
        {
            "schema_version": 1,
            "records": {
                "T-ONE": {"file": "T-ONE-record.json", "sha256": sha256((tmp_path / relative).read_bytes())}
            },
        },
    )
    with pytest.raises(FactoryError, match="must be an object"):
        load_result(tmp_path, "M-ONE", "T-ONE")


def test_payload_has_no_unreferenced_installed_templates():
    paths = kernel_payload()
    assert not any(p.startswith(".factory/templates/installed-") for p in paths)
    assert b"--no-dev" in paths[".factory/README.md"]


def test_payload_ships_the_orchestrator_guard_and_starter_defaults():
    paths = kernel_payload()
    assert ".factory/hooks/orchestrator_guard.py" in paths
    assert not any("__pycache__" in p or p.endswith(".pyc") for p in paths)
    config = starter("product", "claude")
    assert (
        config["model_selection"]["mode"] == "roles"
        and config["model_selection"]["roles"]["claude"]["reviewer"] == "opus"
    )
    assert config["enforcement"] == {"claude_orchestrator_agent": True}
    assert config["limits"]["high_risk_lines"] == 400
    policy = json.loads(paths[".factory/policy.json"])
    assert "tests/**" in policy["test_paths"] and ".factory/hooks/**" in policy["protected_paths"]
    for profile in ("claude", "codex", "copilot"):
        capabilities = json.loads(paths[f".factory/vendors/{profile}.json"])["capabilities"]
        assert set(capabilities) == {
            "subagents",
            "tool_allowlists",
            "blocking_hooks",
            "orchestrator_enforcement",
        }
        assert capabilities["blocking_hooks"] in ("none", "guardrail", "fail_closed_when_run")
        assert capabilities["orchestrator_enforcement"] in ("hook", "tool_allowlist", "instructions")
