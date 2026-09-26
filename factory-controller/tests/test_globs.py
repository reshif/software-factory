import pytest

from factory.globs import match


@pytest.mark.parametrize("path,pattern,expected", [
    ("src/auth/login.py", "**/auth/**", True),
    ("auth/login.py", "**/auth/**", True),
    ("src/authz/login.py", "**/auth/**", False),
    (".env", "**/.env", True),
    ("config/.env", "**/.env", True),
    ("src/a/b/c.py", "src/**", True),
    ("srcx/a.py", "src/**", False),
    ("tests/conftest.py", "**/conftest.py", True),
    ("README.md", "**/*.md", True),
    ("docs/guide.md", "docs/**", True),
    (".github/workflows/ci.yml", ".github/**", True),
    ("requirements-dev.txt", "requirements*.txt", True),
    ("a/b.py", "*.py", False),
    ("b.py", "?.py", True),
])
def test_glob_match(path, pattern, expected):
    assert match(path, pattern) is expected
