"""First-run help: static check detection, maintainer defaults and remaining setup steps.

Nothing here executes project code: detection reads file names and manifests only.
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path

from .core import FactoryError, git

# Matches limits.check_timeout_seconds in new installations; the runner uses the smaller value.
CHECK_TIMEOUT_SECONDS = 300
PLACEHOLDER_CHECK = "configure-me"
INITIAL_COMMIT_MESSAGE = "Initialize software-factory"
COMMIT_COMMAND = f'git add -A && git commit -m "{INITIAL_COMMIT_MESSAGE}"'
MAKEFILES = ("GNUmakefile", "makefile", "Makefile")
PYTHON_MANIFESTS = ("pyproject.toml", "requirements.txt", "requirements-dev.txt", "setup.cfg", "setup.py")
# npm init writes this test script; it always fails, so it is no product check.
NPM_PLACEHOLDER_TEST = re.compile(r"no test specified", re.IGNORECASE)
MAKE_TEST_TARGET = re.compile(r"^test\s*:(?!=)", re.MULTILINE)
CLIENT_LABELS = {"claude": "Claude Code", "codex": "Codex", "copilot": "Copilot"}
CLIENT_PREFIX = {"claude": "/", "codex": "$", "copilot": "/"}


def _check(id: str, command: list[str], source: str, confident: bool = True) -> dict:
    return {
        "check": {
            "id": id,
            "command": command,
            "cwd": ".",
            "required": True,
            "timeout_seconds": CHECK_TIMEOUT_SECONDS,
        },
        "detected_from": source,
        "confident": confident,
    }


def _read(path: Path, limit: int = 1_000_000) -> str | None:
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
            return None
        return path.read_text(errors="replace")
    except OSError:
        return None


def _python_tests(root: Path) -> list[str]:
    evidence = [name for name in PYTHON_MANIFESTS if (root / name).is_file()]
    evidence += [name + "/" for name in ("tests", "test") if (root / name).is_dir()]
    try:
        evidence += sorted(
            p.name for p in root.iterdir() if p.is_file() and re.fullmatch(r"test_.*\.py|.*_test\.py", p.name)
        )[:3]
    except OSError:
        pass
    return evidence


def detect_checks(root: Path) -> list[dict]:
    """Suggest checks from project files; `confident` ones are what init writes to factory.json."""
    root = Path(root)
    if not root.is_dir():
        return []
    found = []
    makefile = next((root / n for n in MAKEFILES if (root / n).is_file()), None)
    make_text = _read(makefile) if makefile else None
    make_test = bool(make_text and MAKE_TEST_TARGET.search(make_text))
    # A Makefile test target is the project's own entry point; language runners stay suggestions.
    language_confident = not make_test
    if make_test:
        found.append(_check("make-test", ["make", "test"], f"{makefile.name} test target"))
    python = _python_tests(root)
    if (root / "uv.lock").is_file():
        found.append(
            _check("python-tests", ["uv", "run", "--locked", "pytest"], "uv.lock", language_confident)
        )
    elif python:
        found.append(
            _check("python-tests", ["python", "-m", "pytest"], ", ".join(python), language_confident)
        )
    package = _read(root / "package.json")
    if package is not None:
        try:
            scripts = json.loads(package).get("scripts") or {}
        except (ValueError, AttributeError):
            scripts = {}
        scripts = scripts if isinstance(scripts, dict) else {}
        runner = (
            "pnpm"
            if (root / "pnpm-lock.yaml").is_file()
            else "yarn"
            if (root / "yarn.lock").is_file()
            else "npm"
        )
        test = scripts.get("test")
        if isinstance(test, str) and test.strip() and not NPM_PLACEHOLDER_TEST.search(test):
            found.append(
                _check("node-tests", [runner, "test"], "package.json scripts.test", language_confident)
            )
        for name in ("lint", "typecheck", "build"):
            if isinstance(scripts.get(name), str):
                found.append(
                    _check(f"node-{name}", [runner, "run", name], f"package.json scripts.{name}", False)
                )
    if (root / "go.mod").is_file():
        found.append(_check("go-tests", ["go", "test", "./..."], "go.mod", language_confident))
    if (root / "Cargo.toml").is_file():
        found.append(_check("rust-tests", ["cargo", "test"], "Cargo.toml", language_confident))
    return found


def suggestion_view(detected: list[dict]) -> list[dict]:
    return [
        {**item["check"], "detected_from": item["detected_from"], "written_by_init": item["confident"]}
        for item in detected
    ]


def default_maintainer(root: Path) -> str | None:
    """The Git identity of whoever runs init: user.name, else user.email; None when unset."""
    where = root if Path(root).is_dir() else Path.cwd()
    for key in ("user.name", "user.email"):
        try:
            value = git(where, "config", "--get", key, check=False).strip()
        except FactoryError:
            return None
        if value:
            return value
    return None


def git_identity_missing(root: Path) -> list[str]:
    where = root if Path(root).is_dir() else Path.cwd()
    return [key for key in ("user.name", "user.email") if not git(where, "config", "--get", key, check=False)]


def has_commit(root: Path) -> bool:
    try:
        return bool(git(root, "rev-parse", "--verify", "-q", "HEAD", check=False))
    except FactoryError:
        return False


def is_repository_root(root: Path) -> bool:
    try:
        top = git(root, "rev-parse", "--show-toplevel", check=False)
    except FactoryError:
        return False
    return bool(top) and Path(top).resolve() == Path(root).resolve()


def uncommitted(root: Path, pathspecs) -> bool:
    specs = sorted(set(pathspecs))
    if not specs:
        return False
    try:
        return bool(git(root, "status", "--porcelain", "--untracked-files=all", "--", *specs, check=False))
    except FactoryError:
        return True


# Installation files; mission records under .factory/missions are ordinary work, not setup.
FACTORY_PATHS = ("factory.json", "factory.lock.json", ".factory", ".gitignore", ":(exclude).factory/missions")


def start_instructions(profile_names: list[str]) -> list[str]:
    return [
        f"Open {CLIENT_LABELS[p]} in this project and run {CLIENT_PREFIX[p]}factory-build"
        for p in profile_names
        if p in CLIENT_LABELS
    ]


def next_steps(
    root: Path,
    config: dict,
    profile_names: list[str],
    *,
    detected: list[dict] | None = None,
    runtime_missing: bool = False,
    exports_stale: bool = False,
    cwd: Path | None = None,
) -> list[str]:
    """Ordered, copy-pasteable actions left before the first mission; [] when only starting remains."""
    root = Path(root)
    steps = []
    checks = config.get("checks", [])
    edited = False
    if any(c.get("id") == PLACEHOLDER_CHECK for c in checks):
        steps.append(
            "Edit factory.json: replace the failing configure-me check with your product's test command "
            '(an argv array, e.g. "command": ["python", "-m", "pytest"])'
        )
        edited = True
    elif detected:
        shown = "; ".join(shlex.join(item["check"]["command"]) for item in detected if item["confident"])
        steps.append(
            f"Review the detected checks in factory.json (not run yet): {shown}; run them once yourself "
            "and adjust factory.json if they fail"
        )
    if not config.get("owners", {}).get("maintainer"):
        steps.append('Edit factory.json: set owners.maintainer to your name, e.g. "maintainer": "Your Name"')
        edited = True
    if runtime_missing:
        steps.append("uv sync --locked --no-dev --project .factory")
    if edited or exports_stale:
        steps.append("software-factory render")
    if not is_repository_root(root):
        steps.append(f"git init -b main && {COMMIT_COMMAND}")
    elif not has_commit(root) or edited or exports_stale or uncommitted(root, FACTORY_PATHS):
        steps.append(COMMIT_COMMAND)
    if not steps:
        return []
    here = Path(cwd or Path.cwd()).absolute()
    if here.resolve() != root.resolve():
        steps.insert(0, "cd " + shlex.quote(str(root)))
    steps.extend(start_instructions(profile_names))
    return steps
