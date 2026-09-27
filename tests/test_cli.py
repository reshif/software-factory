import json
from unittest.mock import patch

import pytest

from software_factory.cli import main


def test_project_command_dispatches_before_global_parser(tmp_path):
    with (
        patch("software_factory.cli._dispatch", return_value=7) as dispatch,
        patch("software_factory.cli.build_parser") as parser,
        pytest.raises(SystemExit) as exc,
    ):
        main(["future-project-command", "--future-option", "value", "--root", str(tmp_path)])
    assert exc.value.code == 7
    parser.assert_not_called()
    assert dispatch.call_args.args[1] == "future-project-command"


def test_disagreeing_init_paths_refused(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["init", str(tmp_path / "one"), "--root", str(tmp_path / "two"), "--profile", "codex"])
    assert exc.value.code != 0
    assert "disagree" in json.loads(capsys.readouterr().err)["error"]
    assert not (tmp_path / "one").exists()
    assert not (tmp_path / "two").exists()


def test_json_flag_is_not_accepted(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["version", "--json", "--root", str(tmp_path)])
    assert exc.value.code == 2
    assert "unrecognized arguments: --json" in capsys.readouterr().err


def test_to_version_is_upgrade_only(tmp_path, capsys):
    from software_factory.cli import build_parser

    with pytest.raises(SystemExit) as exc:
        main(["init", str(tmp_path / "project"), "--to", "0.0.1"])
    assert exc.value.code == 2
    assert "unrecognized arguments: --to" in capsys.readouterr().err
    assert not (tmp_path / "project").exists()
    assert build_parser().parse_args(["upgrade", "--to", "1.2.3"]).to_version == "1.2.3"


def pinned_project(root, launcher="raise SystemExit(0)\n", version="0.0.1"):
    import os
    import sys

    (root / ".factory/src/software_factory").mkdir(parents=True)
    (root / ".factory/src/software_factory/cli.py").write_text("")
    (root / ".factory/run.py").write_text(launcher)
    (root / ".factory/installation.json").write_text(
        json.dumps({"schema_version": 2, "runtime": "python-uv", "version": version, "files": {}})
    )
    bin_dir = root / ".factory/.venv/bin"
    bin_dir.mkdir(parents=True)
    os.symlink(sys.executable, bin_dir / "python")
    return bin_dir / "python"


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX process replacement")
def test_dispatch_replaces_process_on_posix(tmp_path):
    from software_factory.cli import _dispatch

    python = pinned_project(tmp_path)
    with (
        patch("software_factory.cli.os.execv", side_effect=SystemExit(0)) as execv,
        patch("software_factory.cli.os.chdir") as chdir,
        patch("software_factory.cli.subprocess.run") as run,
        pytest.raises(SystemExit),
    ):
        _dispatch(tmp_path, "status", ["status", "--root", "relative/elsewhere", "--flag"])
    run.assert_not_called()
    chdir.assert_called_once_with(tmp_path)
    executable, argv = execv.call_args.args
    assert executable == str(python)
    assert argv[:3] == [str(python), "-I", "-B"]
    assert argv[3] == str(tmp_path / ".factory/run.py")
    assert argv[4:] == ["status", f"--root={tmp_path}", "--flag"]


def test_dispatch_uses_subprocess_on_windows(tmp_path):
    from software_factory.cli import _dispatch

    pinned_project(tmp_path)
    with (
        patch("software_factory.cli.REPLACE_PROCESS", False),
        patch("software_factory.cli.os.execv") as execv,
        patch("software_factory.cli.subprocess.run") as run,
    ):
        run.return_value.returncode = 3
        assert _dispatch(tmp_path, "status", ["status"]) == 3
    execv.assert_not_called()
    assert run.call_args.kwargs["cwd"] == tmp_path
    assert "-B" in run.call_args.kwargs["args"]


@pytest.mark.skipif(__import__("os").name == "nt", reason="POSIX signals")
def test_sigterm_reaches_pinned_runtime(tmp_path):
    import os
    import signal
    import subprocess
    import sys
    import time

    marker = tmp_path / "pinned.pid"
    pinned_project(
        tmp_path,
        launcher=(
            "import os, pathlib, time\n"
            f"pathlib.Path({str(marker)!r}).write_text(str(os.getpid()))\n"
            "time.sleep(60)\n"
        ),
    )
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    process = subprocess.Popen(
        [sys.executable, "-m", "software_factory", "long-command", "--root", str(tmp_path)], env=env
    )
    try:
        deadline = time.monotonic() + 30
        while not marker.exists() or not marker.read_text():
            assert process.poll() is None, "dispatch exited early"
            assert time.monotonic() < deadline, "pinned runtime did not start"
            time.sleep(0.05)
        # The pinned runtime is the CLI process itself, so the signal reaches it.
        assert int(marker.read_text()) == process.pid
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == -signal.SIGTERM
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def test_pending_journal_refuses_mutating_dispatch(tmp_path, capsys):
    from software_factory.cli import _dispatch
    from software_factory.core import write_json
    from software_factory.transactions import JOURNAL

    pinned_project(tmp_path)
    write_json(tmp_path, JOURNAL, {"schema_version": 1, "files": {}})
    for command in ("render", "mission", "uninstall", "checks", "future-command"):
        with pytest.raises(Exception, match="interrupted.*recover"):
            _dispatch(tmp_path, command, [command])
    with (
        patch("software_factory.cli.os.execv", side_effect=SystemExit(0)) as execv,
        pytest.raises(SystemExit),
    ):
        _dispatch(tmp_path, "status", ["status"])
    assert execv.called
    assert _dispatch(tmp_path, "recover", ["recover"]) is None


def test_uninstalled_project_explains_reinstall(tmp_path, capsys):
    pinned_project(tmp_path)
    manifest = tmp_path / ".factory/installation.json"
    value = json.loads(manifest.read_text())
    value["uninstalled"] = True
    manifest.write_text(json.dumps(value))
    with pytest.raises(SystemExit) as exc:
        main(["status", "--root", str(tmp_path)])
    assert exc.value.code == 1
    error = json.loads(capsys.readouterr().err)["error"]
    assert "uninstalled" in error and "init" in error and "uv sync" not in error
    assert "software-factory upgrade when reinstalling a different release" in error
    with pytest.raises(SystemExit):
        main(["doctor", "--root", str(tmp_path)])
    issues = json.loads(capsys.readouterr().out)["issues"]
    assert any(i["code"] == "uninstalled" and "upgrade when reinstalling" in i["message"] for i in issues)


def test_git_process_errors_are_reported(tmp_path, capsys):
    import subprocess

    with (
        patch(
            "software_factory.cli.inspect_project",
            side_effect=subprocess.CalledProcessError(128, ["git", "init"]),
        ),
        pytest.raises(SystemExit) as exc,
    ):
        main(["inspect", "--root", str(tmp_path)])
    assert exc.value.code == 1
    assert "git" in json.loads(capsys.readouterr().err)["error"]


def test_allow_downgrade_is_upgrade_only():
    from software_factory.cli import build_parser

    parser = build_parser()
    assert parser.parse_args(["upgrade", "--allow-downgrade"]).allow_downgrade is True
    with pytest.raises(SystemExit):
        parser.parse_args(["init", "--allow-downgrade"])


@pytest.mark.parametrize("option", ["--r", "--ro", "--roo"])
def test_abbreviated_root_cannot_initialize_the_wrong_project(tmp_path, monkeypatch, option):
    current = tmp_path / "current"
    requested = tmp_path / "requested"
    current.mkdir()
    monkeypatch.chdir(current)
    with patch("software_factory.installation.install") as install, pytest.raises(SystemExit) as exc:
        main([option, str(requested), "init", "--profile", "codex"])
    assert exc.value.code == 2
    install.assert_not_called()
    assert not requested.exists()
    assert list(current.iterdir()) == []


def _subparsers(parser):
    import argparse

    return next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)


def _help(capsys, *argv):
    with pytest.raises(SystemExit) as exc:
        main([*argv, "--help"])
    assert exc.value.code == 0
    return capsys.readouterr().out


def test_root_help_describes_and_groups_every_command(capsys):
    from software_factory.cli import build_parser

    text = _help(capsys)
    assert "usage: software-factory [--root PATH] <command> [options]" in text
    assert "{" not in text.split("Examples:")[0]
    names = [name for name in _subparsers(build_parser()).choices if name != "jev"]
    for name in names:
        lines = [line for line in text.splitlines() if line.split()[:1] == [name]]
        assert lines and len(lines[0].split()) > 2, name
    sections = {title: text.index(title + ":\n") for title in ("Setup", "Mission workflow", "Models & JEV")}
    groups = {
        "Setup": [
            "init",
            "upgrade",
            "uninstall",
            "recover",
            "render",
            "doctor",
            "inspect",
            "version",
            "auth",
        ],
        "Mission workflow": ["mission", "status", "checks", "verify", "gate", "packet"],
        "Models & JEV": ["models", "semantic (jev)", "triage"],
    }
    assert sorted(n for group in groups.values() for n in group) == sorted(
        [n for n in names if n != "semantic"] + ["semantic (jev)"]
    )
    ordered = sorted(sections.values())
    for title, members in groups.items():
        start = sections[title]
        end = min([i for i in ordered if i > start] + [text.index("Global option:")])
        body = text[start:end]
        assert [line.split("  ")[1].strip() for line in body.splitlines()[1:] if line.strip()] == members
    assert "--root PATH" in text.split("Examples:")[0].split("Global option:")[1]
    examples = text.split("Examples:")[1]
    for snippet in ("init", "doctor", "models discover --profile claude", "models plan", "/factory-build"):
        assert snippet in examples
    assert "$factory-build" in examples


def test_every_subcommand_and_option_has_help(capsys):
    from software_factory.cli import build_parser

    def walk(parser, path):
        sub = _subparsers(parser)
        for action in parser._actions:
            if action is not sub and action.dest not in ("help", "version"):
                assert action.help, (path, action.dest)
        if sub is None:
            return
        helped = {choice.dest for choice in sub._choices_actions if choice.help}
        for name, child in sub.choices.items():
            if name != "jev":
                assert name in helped, (path, name)
            walk(child, [*path, name])

    walk(build_parser(), [])
    text = _help(capsys, "models")
    from software_factory.models import MODEL_COMMANDS

    for name in MODEL_COMMANDS:
        assert f"\n  {name} " in text
    for flag in ("--picker TEXT", "--client-version VERSION", "--session LABEL", "--output PATH"):
        assert flag in text
    epilog = text.split("Claude Code example")[1]
    assert "models discover --profile claude" in epilog and "--picker" in epilog
    assert epilog.index("discover") < epilog.index("models plan") < epilog.index("models validate")
