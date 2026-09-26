"""factory-kit/README.md: final-review documentation requirements (spec §3 B6)."""


def test_readme_has_the_exact_sandbox_build_command(kit_dir):
    text = (kit_dir / "README.md").read_text()
    assert "docker build -t factory-sandbox:latest -f factory-controller/deploy/sandbox/Dockerfile ." in text


def test_readme_states_the_kit_gate_automation_is_deferred_to_phase_3(kit_dir):
    text = (kit_dir / "README.md").read_text()
    assert "Deferred to Phase 3" in text
    assert "evals" in text.lower()
    assert "CODEOWNERS" in text
    assert "policies/**" in text


def test_readme_documents_no_agent_gets_bash(kit_dir):
    text = (kit_dir / "README.md").read_text()
    assert "Bash" in text
    assert "ToolGuard" in text or "tool_guard" in text
