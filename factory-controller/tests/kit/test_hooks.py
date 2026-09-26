"""The forbidden-path PreToolUse hook: defense in depth (final draft §13, spec §3 B6).

Covers the R-A6 red-team findings: path-normalization bypasses
(`/work/.github/...`, `src/../.github/...`), a case-variant bypass
(`.GitHub/...`), the matcher missing MultiEdit/NotebookEdit, and fail-open
behavior on malformed input for the tools this hook guards (must now fail
closed instead). Also covers C6: the hook's forbidden-path list and glob
algorithm must agree with the controller's own (`floor.yaml` /
`factory.globs`).
"""
import importlib.util
import itertools
import json
import subprocess
import sys

import pytest

from factory import globs as factory_globs
from factory.policy.loader import load_policy


def _run_raw(plugin_dir, payload, cwd, *, env=None):
    hook = plugin_dir / "hooks" / "check_forbidden_paths.py"
    return subprocess.run(
        [sys.executable, str(hook)],
        input=payload, capture_output=True, text=True, cwd=cwd, env=env,
    )


def _run_hook(plugin_dir, tool_name, file_path, cwd, *, env=None):
    payload = json.dumps({"tool_name": tool_name, "tool_input": {"file_path": file_path}})
    return _run_raw(plugin_dir, payload, cwd, env=env)


@pytest.fixture(scope="module")
def hook_module(plugin_dir):
    path = plugin_dir / "hooks" / "check_forbidden_paths.py"
    spec = importlib.util.spec_from_file_location("factory_kit_check_forbidden_paths", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ── contract shape ──────────────────────────────────────────────────────────

def test_hooks_json_matches_the_required_contract(plugin_dir):
    hooks = json.loads((plugin_dir / "hooks" / "hooks.json").read_text())
    pre = hooks["hooks"]["PreToolUse"]
    assert len(pre) == 1
    assert pre[0]["matcher"] == "Write|Edit|MultiEdit|NotebookEdit"
    handler = pre[0]["hooks"][0]
    assert handler["type"] == "command"
    assert handler["command"] == "${CLAUDE_PLUGIN_ROOT}/hooks/check_forbidden_paths.py"


def test_hook_script_is_executable_with_a_python3_shebang(plugin_dir):
    hook = plugin_dir / "hooks" / "check_forbidden_paths.py"
    assert hook.is_file()
    first_line = hook.read_text().splitlines()[0]
    assert first_line == "#!/usr/bin/env python3"
    mode = hook.stat().st_mode
    assert mode & 0o111, "hook script must be executable (mode has no exec bit)"


# ── baseline: blocks a forbidden path, allows a normal one ─────────────────

def test_hook_blocks_a_forbidden_path(plugin_dir, tmp_path):
    result = _run_hook(plugin_dir, "Write", ".github/workflows/ci.yml", tmp_path)
    assert result.returncode == 2
    assert "forbidden" in result.stderr.lower()
    assert result.stderr.strip()  # exit 2 must carry a reason


def test_hook_blocks_a_nested_forbidden_path(plugin_dir, tmp_path):
    result = _run_hook(plugin_dir, "Edit", "policies/floor.yaml", tmp_path)
    assert result.returncode == 2


def test_hook_blocks_an_absolute_forbidden_path_inside_the_project(plugin_dir, tmp_path):
    abs_path = str(tmp_path / ".github" / "workflows" / "ci.yml")
    result = _run_hook(plugin_dir, "Write", abs_path, tmp_path)
    assert result.returncode == 2


def test_hook_allows_a_normal_path(plugin_dir, tmp_path):
    result = _run_hook(plugin_dir, "Write", "src/app/store.py", tmp_path)
    assert result.returncode == 0
    assert result.stderr == ""


def test_hook_ignores_calls_to_tools_it_does_not_guard(plugin_dir, tmp_path):
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})
    result = _run_raw(plugin_dir, payload, tmp_path)
    assert result.returncode == 0


# ── R-A6: bypasses that must now be blocked ─────────────────────────────────

def test_hook_blocks_an_absolute_path_rooted_outside_the_project(plugin_dir, tmp_path):
    """`/work/.github/...` while the actual project root is `tmp_path`: the old
    fallback (raw absolute string, unmatched by the anchored regex) let this
    through. It must now fail closed as unresolvable-inside-the-project."""
    result = _run_hook(plugin_dir, "Write", "/work/.github/workflows/ci.yml", tmp_path)
    assert result.returncode == 2
    assert "does not resolve" in result.stderr.lower() or "forbidden" in result.stderr.lower()


def test_hook_blocks_a_relative_path_traversal_into_github(plugin_dir, tmp_path):
    result = _run_hook(plugin_dir, "Write", "src/../.github/workflows/ci.yml", tmp_path)
    assert result.returncode == 2


def test_hook_blocks_a_case_variant_of_dot_github(plugin_dir, tmp_path):
    result = _run_hook(plugin_dir, "Write", ".GitHub/workflows/ci.yml", tmp_path)
    assert result.returncode == 2


def test_hook_blocks_a_case_variant_of_a_forbidden_extension(plugin_dir, tmp_path):
    result = _run_hook(plugin_dir, "Write", "secrets/PROD.PEM", tmp_path)
    assert result.returncode == 2


@pytest.mark.parametrize("tool_name,input_key", [("MultiEdit", "file_path"), ("NotebookEdit", "notebook_path")])
def test_matcher_now_covers_multiedit_and_notebookedit(plugin_dir, tmp_path, tool_name, input_key):
    payload = json.dumps({"tool_name": tool_name, "tool_input": {input_key: ".github/x"}})
    result = _run_raw(plugin_dir, payload, tmp_path)
    assert result.returncode == 2


def test_notebookedit_on_a_normal_path_is_allowed(plugin_dir, tmp_path):
    payload = json.dumps({"tool_name": "NotebookEdit", "tool_input": {"notebook_path": "notebooks/x.ipynb"}})
    result = _run_raw(plugin_dir, payload, tmp_path)
    assert result.returncode == 0


def test_multiedit_on_a_normal_path_is_allowed(plugin_dir, tmp_path):
    payload = json.dumps({"tool_name": "MultiEdit", "tool_input": {"file_path": "src/app.py", "edits": []}})
    result = _run_raw(plugin_dir, payload, tmp_path)
    assert result.returncode == 0


# ── R-A6: fail CLOSED (not open) on malformed input for guarded tools ───────

def test_hook_fails_closed_on_malformed_stdin(plugin_dir, tmp_path):
    result = _run_raw(plugin_dir, "not json", tmp_path)
    assert result.returncode == 2
    assert result.stderr.strip()


def test_hook_fails_closed_on_a_non_object_payload(plugin_dir, tmp_path):
    result = _run_raw(plugin_dir, json.dumps([1, 2, 3]), tmp_path)
    assert result.returncode == 2


def test_hook_fails_closed_when_tool_input_is_missing(plugin_dir, tmp_path):
    payload = json.dumps({"tool_name": "Write"})
    result = _run_raw(plugin_dir, payload, tmp_path)
    assert result.returncode == 2


def test_hook_fails_closed_when_tool_input_is_not_an_object(plugin_dir, tmp_path):
    payload = json.dumps({"tool_name": "Write", "tool_input": "oops"})
    result = _run_raw(plugin_dir, payload, tmp_path)
    assert result.returncode == 2


def test_hook_fails_closed_when_the_guarded_tool_has_no_path(plugin_dir, tmp_path):
    payload = json.dumps({"tool_name": "Write", "tool_input": {"content": "hi"}})
    result = _run_raw(plugin_dir, payload, tmp_path)
    assert result.returncode == 2


def test_hook_respects_claude_project_dir_env_var(plugin_dir, tmp_path):
    """With CLAUDE_PROJECT_DIR set, resolution is against that root, not cwd."""
    import os

    other_cwd = tmp_path / "elsewhere"
    other_cwd.mkdir()
    env = dict(os.environ, CLAUDE_PROJECT_DIR=str(tmp_path))
    result = _run_hook(plugin_dir, "Write", ".github/workflows/ci.yml", other_cwd, env=env)
    assert result.returncode == 2


# ── C6: parity with the controller's own policy and glob algorithm ─────────

def test_forbidden_patterns_match_the_authoritative_floor_policy(hook_module):
    policy = load_policy()
    assert hook_module.FORBIDDEN_PATTERNS == tuple(policy.floor.forbidden_paths)


MATRIX_PATHS = (
    "", ".github", ".github/workflows/ci.yml", "src/.github/x", "policy/x.yaml",
    "policies/floor.yaml", "evals/case.yaml", "holdouts/scenarios/x.yaml",
    ".env", ".env.local", "a/.env", "secrets/prod.pem", "id_rsa.key",
    "src/app.py", "tests/test_x.py", "README.md", "app/store.py",
    "nested/deeply/.env.production", "keys/service.pem.bak",
)
MATRIX_PATTERNS = (
    ".github/**", "policy/**", "policies/**", "evals/**", "holdouts/**",
    "**/.env", "**/.env.*", "**/*.pem", "**/*.key",
)


@pytest.mark.parametrize("path,pattern", list(itertools.product(MATRIX_PATHS, MATRIX_PATTERNS)))
def test_hook_glob_match_agrees_with_factory_globs(hook_module, path, pattern):
    assert hook_module.glob_match(path, pattern) == factory_globs.match(path, pattern)
