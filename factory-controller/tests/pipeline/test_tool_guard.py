"""`pipeline.tool_guard.ToolGuard` (build spec §3 B7 "Tool guard")."""
import os

import pytest

from factory.pipeline.tool_guard import ToolGuard


@pytest.fixture
def guard(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / "tests" / "conftest.py").write_text("# fixtures")
    return ToolGuard(workdir=str(tmp_path), owned_paths=("app/**", "tests/**"),
                     forbidden_globs=(".github/**",), protected_globs=("**/conftest.py",))


def test_denies_bash_outright(guard):
    assert guard("Bash", {"command": "echo hi"}) is False


def test_denies_websearch_and_webfetch_outright(guard):
    assert guard("WebSearch", {"query": "x"}) is False
    assert guard("WebFetch", {"url": "http://example.com"}) is False


def test_allows_read_inside_workdir(guard, tmp_path):
    assert guard("Read", {"file_path": str(tmp_path / "app" / "x.py")}) is True


def test_denies_read_outside_workdir(guard, tmp_path):
    outside = tmp_path.parent / "secret.txt"
    assert guard("Read", {"file_path": str(outside)}) is False


def test_denies_read_via_dotdot_escape(guard, tmp_path):
    escape = os.path.join(str(tmp_path), "app", "..", "..", "escaped.txt")
    assert guard("Read", {"file_path": escape}) is False


def test_denies_read_via_symlink_escape(guard, tmp_path):
    outside_dir = tmp_path.parent / "outside"
    outside_dir.mkdir(exist_ok=True)
    secret = outside_dir / "secret.txt"
    secret.write_text("nope")
    link = tmp_path / "app" / "escape_link"
    link.symlink_to(secret)
    assert guard("Read", {"file_path": str(link)}) is False


def test_allows_edit_inside_owned_paths(guard, tmp_path):
    assert guard("Edit", {"file_path": str(tmp_path / "app" / "x.py")}) is True


def test_denies_edit_outside_owned_paths(guard, tmp_path):
    # Inside the workdir, but not under app/** or tests/**.
    (tmp_path / "docs").mkdir()
    assert guard("Edit", {"file_path": str(tmp_path / "docs" / "x.md")}) is False


def test_denies_edit_on_forbidden_path_even_if_globbed_elsewhere(guard, tmp_path):
    assert guard("Edit", {"file_path": str(tmp_path / ".github" / "workflows" / "ci.yml")}) is False


def test_denies_write_on_protected_path(guard, tmp_path):
    assert guard("Write", {"file_path": str(tmp_path / "tests" / "conftest.py")}) is False


def test_allows_write_of_a_new_test_file(guard, tmp_path):
    assert guard("Write", {"file_path": str(tmp_path / "tests" / "test_new_thing.py")}) is True


def test_glob_and_grep_with_no_path_default_to_workdir(guard):
    assert guard("Glob", {"pattern": "**/*.py"}) is True
    assert guard("Grep", {"pattern": "TODO"}) is True


def test_relative_paths_resolve_against_workdir(guard):
    assert guard("Read", {"file_path": "app/x.py"}) is True
    assert guard("Edit", {"file_path": "../escape.py"}) is False
