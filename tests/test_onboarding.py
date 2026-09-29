"""First-run onboarding: check detection, maintainer default, --commit and next steps."""

import json
import subprocess

import pytest

from software_factory.cli import doctor, inspect_project, main
from software_factory.core import FactoryError, git
from software_factory.installation import install
from software_factory.onboarding import COMMIT_COMMAND, detect_checks, next_steps
from software_factory.rendering import render

UV_PYTEST = ["uv", "run", "--locked", "pytest"]
PY_PYTEST = ["python", "-m", "pytest"]


@pytest.fixture
def identity(tmp_path_factory, monkeypatch):
    """A controlled global Git configuration; returns a writer for its identity."""
    home = tmp_path_factory.mktemp("home")
    monkeypatch.setenv("HOME", str(home))

    def set_identity(name="Ada Lovelace", email="ada@example.com"):
        lines = ["[user]"]
        if name:
            lines.append(f"\tname = {name}")
        if email:
            lines.append(f"\temail = {email}")
        (home / ".gitconfig").write_text("\n".join(lines) + "\n" if len(lines) > 1 else "")

    set_identity()
    return set_identity


def write(root, files):
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)


def confident(root):
    return {
        item["check"]["id"]: item["check"]["command"] for item in detect_checks(root) if item["confident"]
    }


def factory(root):
    return json.loads((root / "factory.json").read_text())


def commits(root):
    return git(root, "rev-list", "--count", "HEAD", check=False)


def calc_project(root):
    write(
        root,
        {
            "calc.py": "def add(a, b):\n    return a + b\n",
            "tests/test_calc.py": "def test_add():\n    pass\n",
        },
    )


# A. Detection (nothing is executed).


@pytest.mark.parametrize(
    ("files", "expected"),
    [
        ({"uv.lock": "", "pyproject.toml": ""}, {"python-tests": UV_PYTEST}),
        ({"pyproject.toml": "[project]\nname='x'\n"}, {"python-tests": PY_PYTEST}),
        ({"requirements.txt": "pytest\n"}, {"python-tests": PY_PYTEST}),
        ({"setup.cfg": ""}, {"python-tests": PY_PYTEST}),
        ({"calc.py": "", "tests/test_calc.py": ""}, {"python-tests": PY_PYTEST}),
        ({"test_calc.py": ""}, {"python-tests": PY_PYTEST}),
        ({"package.json": '{"scripts": {"test": "vitest"}}'}, {"node-tests": ["npm", "test"]}),
        (
            {"package.json": '{"scripts": {"test": "vitest"}}', "pnpm-lock.yaml": ""},
            {"node-tests": ["pnpm", "test"]},
        ),
        (
            {"package.json": '{"scripts": {"test": "jest"}}', "yarn.lock": ""},
            {"node-tests": ["yarn", "test"]},
        ),
        ({"package.json": '{"scripts": {"test": "echo \\"Error: no test specified\\" && exit 1"}}'}, {}),
        ({"package.json": '{"name": "no-scripts"}'}, {}),
        ({"package.json": "{not json"}, {}),
        ({"go.mod": "module x\n"}, {"go-tests": ["go", "test", "./..."]}),
        ({"Cargo.toml": "[package]\n"}, {"rust-tests": ["cargo", "test"]}),
        ({"Makefile": "build:\n\tcc x.c\ntest: build\n\t./x --test\n"}, {"make-test": ["make", "test"]}),
        ({"Makefile": "test := value\nall:\n\ttrue\n"}, {}),
        ({"README.md": "hello\n", "main.c": ""}, {}),
        (
            {"go.mod": "module x\n", "package.json": '{"scripts": {"test": "jest"}}'},
            {"go-tests": ["go", "test", "./..."], "node-tests": ["npm", "test"]},
        ),
    ],
)
def test_detection(tmp_path, files, expected):
    write(tmp_path, files)
    assert confident(tmp_path) == expected
    for item in detect_checks(tmp_path):
        check = item["check"]
        assert check["cwd"] == "." and check["required"] is True and check["timeout_seconds"] >= 60
        assert item["detected_from"]


def test_makefile_test_target_is_preferred_over_language_runners(tmp_path):
    write(tmp_path, {"Makefile": "test:\n\tpytest\n", "pyproject.toml": "", "tests/test_a.py": ""})
    detected = {item["check"]["id"]: item for item in detect_checks(tmp_path)}
    assert confident(tmp_path) == {"make-test": ["make", "test"]}
    assert detected["python-tests"]["confident"] is False


def test_node_lint_scripts_are_suggestions_only(tmp_path):
    write(tmp_path, {"package.json": '{"scripts": {"test": "jest", "lint": "eslint ."}}'})
    detected = {item["check"]["id"]: item for item in detect_checks(tmp_path)}
    assert detected["node-lint"]["check"]["command"] == ["npm", "run", "lint"]
    assert detected["node-lint"]["confident"] is False


def test_inspect_suggests_pytest_for_plain_test_files(tmp_path):
    calc_project(tmp_path)
    suggested = inspect_project(tmp_path)["suggested_checks"]
    assert [(c["id"], c["command"], c["written_by_init"]) for c in suggested] == [
        ("python-tests", PY_PYTEST, True)
    ]


def test_init_writes_detected_checks_without_running_them(tmp_path, identity):
    # A test target that would leave a marker if anything executed it.
    write(tmp_path, {"Makefile": "test:\n\ttouch ran-tests\n"})
    report = install(tmp_path, selected="claude", skip_sync=True)
    assert not (tmp_path / "ran-tests").exists()
    checks = factory(tmp_path)["checks"]
    assert [c["command"] for c in checks] == [["make", "test"]]
    assert report["checks"] == "not_run"
    assert report["detected_checks"][0]["detected_from"] == "Makefile test target"
    assert "not been run" in report["checks_note"]
    assert render(tmp_path, check=True)["ok"]
    assert "configure_checks" not in {i["code"] for i in doctor(tmp_path)["issues"]}


def test_init_keeps_placeholder_when_nothing_detected_or_opted_out(tmp_path, identity):
    empty = tmp_path / "empty"
    empty.mkdir()
    report = install(empty, selected="claude", skip_sync=True)
    assert [c["id"] for c in factory(empty)["checks"]] == ["configure-me"]
    assert report["checks"] == "configuration_required" and "configure-me" in report["checks_note"]
    assert "configure-me" in report["next_steps"][1]
    opted = tmp_path / "opted"
    calc_project(opted)
    report = install(opted, selected="claude", skip_sync=True, detect=False)
    assert [c["id"] for c in factory(opted)["checks"]] == ["configure-me"]
    assert report["detected_checks"] == []
    message = {i["code"]: i for i in doctor(opted)["issues"]}["configure_checks"]["message"]
    assert "python -m pytest" in message


def test_existing_factory_json_is_never_rewritten(tmp_path, identity):
    install(tmp_path, selected="claude", skip_sync=True)
    before = (tmp_path / "factory.json").read_bytes()
    calc_project(tmp_path)
    install(tmp_path, selected="claude", skip_sync=True)
    assert (tmp_path / "factory.json").read_bytes() == before
    with pytest.raises(FactoryError, match="set owners.maintainer there"):
        install(tmp_path, selected="claude", skip_sync=True, maintainer="Someone Else")
    assert (tmp_path / "factory.json").read_bytes() == before


# B. Maintainer.


def test_maintainer_explicit_default_and_fallbacks(tmp_path, identity):
    explicit = tmp_path / "explicit"
    install(explicit, selected="claude", skip_sync=True, maintainer="Grace Hopper")
    assert factory(explicit)["owners"] == {"maintainer": "Grace Hopper", "reviewer": None}
    default = tmp_path / "default"
    install(default, selected="claude", skip_sync=True)
    assert factory(default)["owners"] == {"maintainer": "Ada Lovelace", "reviewer": None}
    identity(name=None)
    email = tmp_path / "email"
    install(email, selected="claude", skip_sync=True)
    assert factory(email)["owners"]["maintainer"] == "ada@example.com"
    identity(name=None, email=None)
    unknown = tmp_path / "unknown"
    report = install(unknown, selected="claude", skip_sync=True)
    assert factory(unknown)["owners"]["maintainer"] is None and report["maintainer"] is None
    assert any("owners.maintainer" in step for step in report["next_steps"])
    assert "software-factory render" in report["next_steps"]
    with pytest.raises(FactoryError, match="must not be empty"):
        install(tmp_path / "blank", selected="claude", skip_sync=True, maintainer=" ")


# C. --commit.


def test_commit_in_plain_folder_creates_repository_and_one_commit(tmp_path, identity):
    calc_project(tmp_path)
    report = install(tmp_path, selected="claude", skip_sync=True, commit=True)
    assert git(tmp_path, "symbolic-ref", "--short", "HEAD") == "main"
    assert commits(tmp_path) == "1"
    assert report["commit"]["created"] and report["commit"]["sha"] == git(tmp_path, "rev-parse", "HEAD")
    assert git(tmp_path, "log", "-1", "--format=%s") == "Initialize software-factory"
    assert git(tmp_path, "status", "--porcelain") == ""
    tracked = git(tmp_path, "ls-files").splitlines()
    assert {"calc.py", "tests/test_calc.py", "factory.json", ".gitignore"} <= set(tracked)
    assert not any(name.startswith((".factory/local/", ".factory/.venv/")) for name in tracked)
    config = factory(tmp_path)
    assert config["checks"][0]["command"] == PY_PYTEST and config["owners"]["maintainer"] == "Ada Lovelace"
    # Only the review of the (unrun) detected checks and the runtime sync remain.
    steps = report["next_steps"]
    assert not any(COMMIT_COMMAND in step or "owners.maintainer" in step for step in steps)
    assert steps[-1] == "Open Claude Code in this project and run /factory-build"

    # A rerun has nothing left to commit and leaves history alone.
    again = install(tmp_path, selected="claude", skip_sync=True, commit=True)
    assert again["commit"] == {"created": False, "reason": "nothing to commit"}
    assert commits(tmp_path) == "1"

    from software_factory.workflow import create_mission

    (tmp_path / "req.md").write_text("Add a subtract function\n")
    mission = create_mission(
        tmp_path, {"id": "M-1", "title": "t", "kind": "patch", "request_file": "req.md"}, require_request=True
    )
    assert mission["id"] == "M-1"
    # Mission records are work, not setup: they do not bring the initial commit step back.
    assert next_steps(tmp_path, factory(tmp_path), ["claude"], cwd=tmp_path) == []


def test_new_repository_without_commits(tmp_path, identity):
    git(tmp_path, "init", "-q")
    calc_project(tmp_path)
    with pytest.raises(FactoryError, match="--commit creates the first commit"):
        install(tmp_path, selected="claude", skip_sync=True)
    install(tmp_path, selected="claude", skip_sync=True, commit=True)
    assert commits(tmp_path) == "1" and git(tmp_path, "status", "--porcelain") == ""


def test_commit_in_existing_history_commits_only_factory_files(tmp_path, identity):
    git(tmp_path, "init", "-q")
    calc_project(tmp_path)
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "user work")
    base = git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "wip.txt").write_text("unfinished")
    with pytest.raises(FactoryError, match="uncommitted"):
        install(tmp_path, selected="claude", skip_sync=True, commit=True)
    with pytest.raises(FactoryError, match="mix them into the factory commit"):
        install(tmp_path, selected="claude", skip_sync=True, commit=True, allow_dirty=True)
    assert not (tmp_path / "factory.json").exists()
    (tmp_path / "wip.txt").unlink()
    report = install(tmp_path, selected="claude", skip_sync=True, commit=True)
    assert report["commit"]["scope"] == "factory files only"
    assert git(tmp_path, "rev-parse", "HEAD~1") == base
    assert git(tmp_path, "status", "--porcelain") == ""


def test_commit_requires_git_identity_before_any_change(tmp_path, identity):
    identity(name=None, email=None)
    calc_project(tmp_path)
    with pytest.raises(FactoryError, match="needs a Git identity.*No changes were made"):
        install(tmp_path, selected="claude", skip_sync=True, commit=True)
    assert not (tmp_path / ".git").exists() and not (tmp_path / "factory.json").exists()


def test_commit_dry_run_writes_nothing(tmp_path, identity):
    calc_project(tmp_path)
    report = install(tmp_path, selected="claude", skip_sync=True, commit=True, dry_run=True)
    assert report["commit"] == {"planned": "Initialize software-factory", "git_init": True}
    assert not (tmp_path / ".git").exists() and not (tmp_path / "factory.json").exists()


def test_upgrade_rejects_commit(tmp_path, identity):
    install(tmp_path, selected="claude", skip_sync=True)
    with pytest.raises(FactoryError, match="init only"):
        install(tmp_path, skip_sync=True, upgrade=True, commit=True, allow_dirty=True)


# D. Next steps.


def test_next_steps_without_commit_and_empty_when_ready(tmp_path, identity):
    calc_project(tmp_path)
    report = install(tmp_path, selected="codex", skip_sync=True)
    steps = report["next_steps"]
    assert steps[0].startswith("cd ")
    assert any("not run yet" in step and "python -m pytest" in step for step in steps)
    assert f"git init -b main && {COMMIT_COMMAND}" in steps
    assert "uv sync --locked --no-dev --project .factory" in steps
    assert steps[-1] == "Open Codex in this project and run $factory-build"
    assert report["start"] == [steps[-1]]
    # Once committed, only starting the client remains.
    git(tmp_path, "init", "-q")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-q", "-m", "init")
    assert next_steps(tmp_path, factory(tmp_path), ["codex"], cwd=tmp_path) == []
    # doctor surfaces the same list (here: the runtime was never synced).
    report = doctor(tmp_path)
    assert report["next_steps"][-2:] == [
        "uv sync --locked --no-dev --project .factory",
        "Open Codex in this project and run $factory-build",
    ]


def test_cli_init_flags(tmp_path, identity, capsys):
    project = tmp_path / "project"
    project.mkdir()
    calc_project(project)
    main(["init", str(project), "--profile", "claude", "--skip-sync", "--commit", "--maintainer", "Grace"])
    report = json.loads(capsys.readouterr().out)
    assert report["commit"]["created"] and report["maintainer"] == "Grace"
    assert report["detected_checks"][0]["command"] == PY_PYTEST
    other = tmp_path / "other"
    other.mkdir()
    calc_project(other)
    main(["init", str(other), "--profile", "claude", "--skip-sync", "--no-detect-checks"])
    assert [c["id"] for c in factory(other)["checks"]] == ["configure-me"]
    capsys.readouterr()
    with pytest.raises(SystemExit):
        main(["upgrade", str(other), "--commit"])
    assert subprocess.run(
        ["git", "-C", str(other), "rev-parse", "HEAD"], check=False, capture_output=True
    ).returncode
