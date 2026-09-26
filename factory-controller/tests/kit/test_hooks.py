"""The forbidden-path PreToolUse hook: defense in depth (final draft §13, spec §3 B6)."""
import json
import subprocess
import sys


def _run_hook(plugin_dir, tool_name, file_path, cwd):
    hook = plugin_dir / "hooks" / "check_forbidden_paths.py"
    payload = json.dumps({"tool_name": tool_name, "tool_input": {"file_path": file_path}})
    return subprocess.run(
        [sys.executable, str(hook)],
        input=payload, capture_output=True, text=True, cwd=cwd,
    )


def test_hooks_json_matches_the_required_contract(plugin_dir):
    hooks = json.loads((plugin_dir / "hooks" / "hooks.json").read_text())
    pre = hooks["hooks"]["PreToolUse"]
    assert len(pre) == 1
    assert pre[0]["matcher"] == "Write|Edit"
    handler = pre[0]["hooks"][0]
    assert handler["type"] == "command"
    assert handler["command"] == "${CLAUDE_PLUGIN_ROOT}/hooks/check_forbidden_paths.py"


def test_hook_script_is_referenced_and_exists(plugin_dir):
    assert (plugin_dir / "hooks" / "check_forbidden_paths.py").is_file()


def test_hook_blocks_a_forbidden_path(plugin_dir, tmp_path):
    result = _run_hook(plugin_dir, "Write", ".github/workflows/ci.yml", tmp_path)
    assert result.returncode == 2
    assert "forbidden" in result.stderr.lower()
    assert result.stderr.strip()  # exit 2 must carry a reason


def test_hook_blocks_a_nested_forbidden_path(plugin_dir, tmp_path):
    result = _run_hook(plugin_dir, "Edit", "policies/floor.yaml", tmp_path)
    assert result.returncode == 2


def test_hook_blocks_an_absolute_forbidden_path(plugin_dir, tmp_path):
    abs_path = str(tmp_path / ".github" / "workflows" / "ci.yml")
    result = _run_hook(plugin_dir, "Write", abs_path, tmp_path)
    assert result.returncode == 2


def test_hook_allows_a_normal_path(plugin_dir, tmp_path):
    result = _run_hook(plugin_dir, "Write", "src/app/store.py", tmp_path)
    assert result.returncode == 0
    assert result.stderr == ""


def test_hook_fails_open_on_malformed_stdin(plugin_dir, tmp_path):
    hook = plugin_dir / "hooks" / "check_forbidden_paths.py"
    result = subprocess.run(
        [sys.executable, str(hook)], input="not json", capture_output=True, text=True, cwd=tmp_path,
    )
    assert result.returncode == 0


def test_hook_ignores_calls_without_a_file_path(plugin_dir, tmp_path):
    hook = plugin_dir / "hooks" / "check_forbidden_paths.py"
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})
    result = subprocess.run(
        [sys.executable, str(hook)], input=payload, capture_output=True, text=True, cwd=tmp_path,
    )
    assert result.returncode == 0
