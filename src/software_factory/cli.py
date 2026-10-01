"""Command entrypoint and pinned project-runtime dispatch."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .core import (
    INVALID_INSTALLATION,
    FactoryError,
    git,
    load_config,
    profiles,
    resolve_root,
    runtime_fingerprint,
    safe_path,
    sha256,
)
from .onboarding import PLACEHOLDER_CHECK, next_steps, start_instructions

# Commands that never write project files; they may run while a recovery journal exists.
READ_ONLY = ("doctor", "inspect", "status")
# Windows has no process replacement with POSIX signal semantics.
REPLACE_PROCESS = os.name != "nt"
UNINSTALLED_MESSAGE = (
    "The factory is uninstalled from this project; run software-factory init to reinstall "
    "(or software-factory upgrade when reinstalling a different release)"
)


def _installation(root: Path) -> dict | None:
    path = root / ".factory/installation.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _runtime_python(root: Path) -> Path:
    return root / ".factory/.venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _running_pinned(root: Path) -> bool:
    local = root / ".factory/src/software_factory"
    return local.is_dir() and Path(__file__).resolve().parent == local.resolve()


def runtime_drift(root: Path, installation: dict) -> list[str]:
    """Compare .factory/src with the ownership hashes recorded at install/upgrade."""
    records = {
        name: record["sha256"]
        for name, record in installation.get("files", {}).items()
        if name.startswith(".factory/src/") and isinstance(record, dict) and "sha256" in record
    }
    drift = []
    for name, expected in records.items():
        path = root / name
        if not path.is_file() or path.is_symlink() or sha256(path.read_bytes()) != expected:
            drift.append(name)
    base = root / ".factory/src"
    if base.is_dir():
        for path in base.rglob("*"):
            if path.is_dir() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            name = path.relative_to(root).as_posix()
            if name not in records:
                drift.append(name)
    return sorted(set(drift))


def drift_message(drift: list[str]) -> str:
    paths = " ".join(drift) if len(drift) <= 10 else ".factory/src"
    shown = ", ".join(drift[:10]) + ("" if len(drift) <= 10 else f" and {len(drift) - 10} more")
    return (
        f"Project runtime source differs from the installed release: {shown}. Restore it with "
        f"git checkout -- {paths} (and delete files the release did not install), or run "
        "software-factory upgrade, which restores deleted runtime files but keeps edited ones"
    )


def inspect_project(root: Path) -> dict:
    from .onboarding import detect_checks, suggestion_view

    suggestions = suggestion_view(detect_checks(root))
    try:
        repository = git(root, "rev-parse", "--show-toplevel")
        baseline = git(root, "rev-parse", "--verify", "HEAD", check=False) or None
    except FactoryError:
        repository, baseline = None, None
    return {
        "root": str(root),
        "repository": repository,
        "baseline": baseline,
        "factory_installed": (root / ".factory/installation.json").is_file(),
        "suggested_checks": suggestions,
        "commands_executed": "read-only Git inspection; no product scripts",
    }


def doctor(root: Path) -> dict:
    from .installation import jev_summary, load_installation
    from .rendering import ENFORCEMENT_FLAGS, enforcement_summary, render
    from .transactions import JOURNAL

    report = inspect_project(root)
    issues = []
    installation = _installation(root)
    manifest_valid = installation is not None
    if installation is not None:
        try:
            load_installation(root)
        except FactoryError as exc:
            manifest_valid = False
            issues.append({"severity": "error", "code": "installation_manifest_invalid", "message": str(exc)})
    local_python = _runtime_python(root)
    pinned = _running_pinned(root)
    if installation is not None:
        report["pinned_version"] = installation.get("version")
    if (root / JOURNAL).is_file():
        issues.append(
            {
                "severity": "error",
                "code": "interrupted_transaction",
                "message": "An interrupted factory operation left a recovery journal; run software-factory "
                "recover to inspect it, then recover --apply",
            }
        )
    uninstalled = bool(installation and installation.get("uninstalled"))
    if uninstalled:
        issues.append(
            {
                "severity": "error",
                "code": "uninstalled",
                "message": UNINSTALLED_MESSAGE,
            }
        )
    # Without the pinned runtime this is a different (global) release; its
    # renderer and fingerprint say nothing about the project runtime.
    skew = (
        installation is not None
        and not uninstalled
        and not pinned
        and installation.get("version") != __version__
    )
    config = None
    try:
        config = load_config(root)
        report["profiles"] = config["profile"]
        report["jev"] = {
            **jev_summary(config),
            "credential": "not_checked",
            "network": "not_checked",
        }
        report["enforcement"] = enforcement_summary(root, config)
        from .rendering import role_model

        selection = config.get("model_selection") or {}
        report["models"] = {
            "mode": selection.get("mode", "inherit"),
            **{
                profile: {
                    role: role_model(config, profile, role) or "inherit (session model)"
                    for role in ("orchestrator", "planner", "implementer", "verifier", "reviewer")
                }
                for profile in profiles(config)
            },
        }
        for profile, flag in ENFORCEMENT_FLAGS.items():
            if (config.get("enforcement") or {}).get(flag) is True and profile not in report["enforcement"]:
                issues.append(
                    {
                        "severity": "warning",
                        "code": "enforcement_inactive",
                        "message": f"enforcement.{flag} has no effect without the {profile} profile",
                    }
                )
        try:
            report["exports"] = "not_checked" if skew or uninstalled else render(root, check=True)
        except FactoryError as exc:
            # Stale exports are one problem among several; the remaining checks still run.
            report["exports"] = "stale"
            issues.append({"severity": "error", "code": "exports_stale", "message": str(exc)})
        if isinstance(report["exports"], dict):
            entry_skills = report["exports"]["relinquished_entry_skills"]
            replaced = sorted(n for n, state in entry_skills.items() if state == "replaced")
            missing = sorted(n for n, state in entry_skills.items() if state == "missing")
            if replaced:
                issues.append(
                    {
                        "severity": "error",
                        "code": "entry_skill_replaced",
                        "message": "Factory entry-point skills (factory-build, factory-blueprint, factory-onboard, factory-retro, "
                        "factory-resume, factory-status) are relinquished and replaced by content the "
                        "factory does not render: "
                        + ", ".join(replaced)
                        + ". To restore, delete the file and run software-factory render",
                    }
                )
            if missing:
                issues.append(
                    {
                        "severity": "warning",
                        "code": "entry_skill_missing",
                        "message": "Factory entry-point skills were deleted: "
                        + ", ".join(missing)
                        + ". To restore, run software-factory render",
                    }
                )
            others = [n for n in report["exports"]["relinquished"] if n not in entry_skills]
            if others:
                issues.append(
                    {
                        "severity": "warning",
                        "code": "exports_relinquished",
                        "message": "Deleted or user-owned factory exports are not managed: "
                        + ", ".join(others)
                        + ". Run software-factory render to recreate deleted ones",
                    }
                )
        report["runtime_fingerprint"] = runtime_fingerprint() if pinned else "not_checked"
        from .crew import status as crew_status

        try:
            knowledge = crew_status(root)
            report["crew"] = {"gaps": knowledge["gaps"]}
            for problem in knowledge["ledger"]["problems"]:
                issues.append({"severity": "warning", "code": "crew_ledger", "message": problem})
        except (FactoryError, OSError, ValueError) as exc:
            report["crew"] = {"error": str(exc)}
        if any(c["id"] == PLACEHOLDER_CHECK for c in config["checks"]):
            suggested = [c for c in report["suggested_checks"] if c["written_by_init"]]
            issues.append(
                {
                    "severity": "warning",
                    "code": "configure_checks",
                    "message": "Replace the placeholder with required product checks in factory.json"
                    + (
                        " (suggested, not run: "
                        + "; ".join(shlex.join(c["command"]) for c in suggested)
                        + ")"
                        if suggested
                        else ""
                    ),
                }
            )
        for owner in ("maintainer", "reviewer"):
            if not config.get("owners", {}).get(owner):
                issues.append(
                    {
                        "severity": "warning",
                        "code": "owner_unconfigured",
                        "message": "Set owners.maintainer in factory.json; the readiness gate refuses "
                        "READY_PR without it"
                        if owner == "maintainer"
                        else f"Set owners.{owner} before relying on team governance",
                    }
                )
        try:
            from .watch import halted
            from .workflow import list_missions

            stop = halted(root)
            if stop:
                issues.append(
                    {
                        "severity": "warning",
                        "code": "factory_halted",
                        "message": f"The factory is halted ({stop.get('reason')}); no new work starts until "
                        "the user runs software-factory mission unhalt",
                    }
                )
            stale = [m for m in list_missions(root)["missions"] if m.get("stale")]
            if stale:
                issues.append(
                    {
                        "severity": "warning",
                        "code": "stale_missions",
                        "message": "Missions without a record change for longer than limits.stale_hours: "
                        + ", ".join(f"{m['id']} ({m['state']}, {m['idle_hours']} h)" for m in stale),
                    }
                )
        except (FactoryError, OSError, ValueError, KeyError):
            pass
    except (FactoryError, OSError) as exc:
        issues.append({"severity": "error", "code": "configuration", "message": str(exc)})
    if manifest_valid and not uninstalled:
        drift = runtime_drift(root, installation)
        if drift:
            issues.append({"severity": "error", "code": "runtime_drift", "message": drift_message(drift)})
    if installation is None:
        issues.append(
            {
                "severity": "error",
                "code": "runtime_missing",
                "message": "The factory is not installed in this project; run software-factory init "
                "--profile <claude,codex,copilot>",
            }
        )
    elif not uninstalled and not local_python.is_file():
        pinned_note = f" (project pins {installation.get('version')})" if installation else ""
        issues.append(
            {
                "severity": "error",
                "code": "runtime_missing",
                "message": f"Run uv sync --locked --no-dev --project .factory{pinned_note}",
            }
        )
    if skew:
        issues.append(
            {
                "severity": "warning",
                "code": "version_skew",
                "message": f"This tool is {__version__} but the project pins {installation.get('version')}; "
                "export and runtime checks need the pinned runtime",
            }
        )
    if not report["baseline"]:
        issues.append(
            {
                "severity": "warning",
                "code": "baseline_required",
                "message": "Set up Git and a reviewed baseline commit before missions or JEV assessments",
            }
        )
    report.update(
        {
            "ok": not any(i["severity"] == "error" for i in issues),
            "version": __version__,
            "python": sys.version.split()[0],
            "uv": shutil.which("uv") is not None,
            "issues": issues,
            "authentication": "not_checked",
            "live_behavior": "not_run",
        }
    )
    codes = {issue["code"] for issue in issues}
    if config is not None and installation is not None and not uninstalled and not skew:
        report["next_steps"] = next_steps(
            root,
            config,
            profiles(config),
            runtime_missing="runtime_missing" in codes,
            exports_stale="exports_stale" in codes,
        )
        report["start"] = start_instructions(profiles(config))
    if not report["ok"]:
        report["_exit_code"] = 2
    return report


class _CommandGroup:
    """Register commands normally and reuse their summaries in grouped root help."""

    def __init__(self, subparsers, title):
        self.subparsers = subparsers
        self.title = title
        self.entries = []

    def add_parser(self, name, *, help, **kwargs):
        kwargs.setdefault("description", help)
        parser = self.subparsers.add_parser(name, help=help, **kwargs)
        aliases = kwargs.get("aliases", [])
        label = f"{name} ({', '.join(aliases)})" if aliases else name
        # Presentation-only actions: parsing still belongs to the real subparser.
        self.entries.append(argparse.Action([], dest=label, help=help))
        return parser


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="software-factory",
        description=(
            f"Software Factory {__version__}\nSet up coding agents, manage missions, and verify project work."
        ),
        usage="%(prog)s [--root PATH] <command> [options]",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="version", version=__version__, help="Show the CLI version")
    parser.add_argument(
        "--root",
        metavar="PATH",
        help="Project directory (default: the nearest factory.json within the current Git repository, "
        "else the current directory); accepted before or after any command",
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>", help=argparse.SUPPRESS)
    setup = _CommandGroup(sub, "Setup")
    missions = _CommandGroup(sub, "Mission workflow")
    assistance = _CommandGroup(sub, "Models & JEV")
    version = setup.add_parser("version", help="Show version and Python runtime details as JSON")
    version.set_defaults(
        handler=lambda a: {"version": __version__, "runtime": "python-uv", "python": sys.version.split()[0]}
    )
    for name, summary in (
        ("inspect", "Read-only: inspect a project and suggest checks"),
        ("doctor", "Diagnose setup, configuration, and runtime problems"),
    ):
        p = setup.add_parser(name, help=summary)
        p.set_defaults(handler=lambda a, n=name: doctor(a.root) if n == "doctor" else inspect_project(a.root))
    from .installation import install, uninstall

    for name in ("init", "upgrade"):
        p = setup.add_parser(
            name,
            help="Initialize a project with native agent profiles"
            if name == "init"
            else "Upgrade a project's installed factory to this release",
        )
        p.add_argument("path", nargs="?", help="Project directory (default: current directory)")
        p.add_argument("--profile", help="Client profiles: claude, codex, copilot, or a comma-separated list")
        p.add_argument("--dry-run", action="store_true", help="Preview changes without writing files")
        p.add_argument("--allow-dirty", action="store_true", help="Allow uncommitted project changes")
        p.add_argument(
            "--skip-sync",
            action="store_true",
            help="Install files only; doctor reports missing runtime until uv sync --locked --no-dev --project .factory",
        )
        p.add_argument("--git-init", action="store_true", help="Create a Git repository if one is missing")
        if name == "init":
            p.add_argument(
                "--maintainer",
                metavar="NAME",
                help="Record owners.maintainer in a new factory.json (default: git config user.name, "
                "else user.email)",
            )
            p.add_argument(
                "--no-detect-checks",
                action="store_true",
                help="Keep the configure-me placeholder instead of writing detected test checks",
            )
            p.add_argument(
                "--commit",
                action="store_true",
                help="Create the Git repository if missing (branch main) and commit the installation as "
                "'Initialize software-factory'; a repository without commits commits all files",
            )
        if name == "upgrade":
            p.add_argument("--to", dest="to_version", help="Require this artifact to be exactly VERSION")
            p.add_argument(
                "--allow-downgrade",
                action="store_true",
                help="Permit replacing a newer installed release with this one",
            )
        p.set_defaults(
            handler=lambda a, n=name: install(
                a.root,
                selected=a.profile,
                dry_run=a.dry_run,
                allow_dirty=a.allow_dirty,
                skip_sync=a.skip_sync,
                git_init=a.git_init,
                upgrade=n == "upgrade",
                to_version=getattr(a, "to_version", None),
                allow_downgrade=getattr(a, "allow_downgrade", False),
                maintainer=getattr(a, "maintainer", None),
                detect=not getattr(a, "no_detect_checks", False),
                commit=getattr(a, "commit", False),
            )
        )
    p = setup.add_parser("uninstall", help="Remove factory-owned files while preserving user changes")
    p.add_argument("--dry-run", action="store_true", help="Preview removals without writing files")
    p.set_defaults(handler=lambda a: uninstall(a.root, dry_run=a.dry_run))
    from .rendering import render

    p = setup.add_parser("render", help="Regenerate native agent instructions")
    p.add_argument("--profile", help="Client profiles: claude, codex, copilot, or a comma-separated list")
    p.add_argument("--check", action="store_true", help="Check generated files without changing them")
    p.add_argument("--dry-run", action="store_true", help="Preview changes without writing files")
    p.set_defaults(handler=lambda a: render(a.root, selected=a.profile, check=a.check, dry_run=a.dry_run))
    from .transactions import recover

    p = setup.add_parser("recover", help="Inspect or recover an interrupted factory file operation")
    p.add_argument("--apply", action="store_true", help="Apply recovery (default: preview only)")
    p.set_defaults(handler=lambda a: recover(a.root, apply_recovery=a.apply))
    from . import auth, models, semantic, triage, workflow

    auth.add_parser(setup)
    models.add_parser(assistance)
    semantic.add_parser(assistance)
    triage.add_parser(assistance)
    workflow.add_parser(missions)
    from . import crew, setup_proposals

    crew.add_parser(missions)
    setup_proposals.add_parser(missions)
    order = ("init", "upgrade", "uninstall", "recover", "render", "doctor", "inspect", "version", "auth")
    setup.entries.sort(key=lambda entry: order.index(entry.dest))
    formatter = argparse.HelpFormatter(parser.prog)
    for group in (setup, missions, assistance):
        formatter.start_section(group.title)
        formatter.add_arguments(group.entries)
        formatter.end_section()
    parser.epilog = formatter.format_help() + (
        "\nGlobal option:\n"
        "  --root PATH     Select the project for any command, e.g. software-factory status --root ~/app\n"
        "\nExamples:\n"
        "  software-factory init . --profile claude,codex\n"
        "  software-factory doctor --root /path/to/project\n"
        "  software-factory models discover --profile claude --session WORK-A \\\n"
        '    --picker "sonnet, opus, haiku, fable" --output .factory/local/models/catalog.json\n'
        "  software-factory models plan --input .factory/local/models/request.json \\\n"
        "    --catalog .factory/local/models/catalog.json --output .factory/local/models/plan.json\n"
        "\nStart work from your coding agent after init: /factory-build in Claude Code or Copilot,\n"
        "$factory-build in Codex. The CLI records and verifies; the agent does the work.\n"
        "\nLearn more:\n"
        "  software-factory <command> --help\n"
        "  software-factory models --help\n"
    )
    return parser


def _common_options(argv: list[str]) -> tuple[list[str], str | None]:
    """Remove the global --root option; everything after ``--`` is passed through untouched."""
    result, root = [], None
    iterator = iter(argv)
    for token in iterator:
        if token == "--":
            result.append(token)
            result.extend(iterator)
        elif token == "--root":
            if root is not None:
                raise FactoryError("Duplicate --root")
            root = next(iterator, None)
            if not root or root.startswith("--"):
                raise FactoryError("--root requires a path")
        elif token.startswith("--root="):
            if root is not None:
                raise FactoryError("Duplicate --root")
            root = token.split("=", 1)[1]
            if not root:
                raise FactoryError("--root requires a path")
        else:
            result.append(token)
    return result, root


def _pinned_arguments(root: Path, raw: list[str]) -> list[str]:
    """Pin --root to the resolved project; the pinned process starts in that root."""
    result, iterator = [], iter(raw)
    for token in iterator:
        if token == "--":
            result.append(token)
            result.extend(iterator)
        elif token == "--root":
            next(iterator, None)
            result.append("--root=" + str(root))
        elif token.startswith("--root="):
            result.append("--root=" + str(root))
        else:
            result.append(token)
    return result


def _requests_help(cleaned: list[str]) -> bool:
    """-h/--help as an option: not the --root value, not after ``--``, not inside ``--opt=value``."""
    options = cleaned[: cleaned.index("--")] if "--" in cleaned else cleaned
    return any(token in ("-h", "--help") for token in options)


def _dispatch(root: Path, command: str, raw: list[str]) -> int | None:
    from .transactions import JOURNAL

    # doctor and inspect always run in this (global) process: diagnosing a project must
    # not execute its repository-controlled runtime.
    if command in ("auth", "recover", "version", "doctor", "inspect") or _requests_help(
        _common_options(raw)[0]
    ):
        return None
    if command not in READ_ONLY and (root / JOURNAL).is_file():
        raise FactoryError(
            "An interrupted factory operation left a recovery journal; run software-factory recover "
            "(then recover --apply) before this command"
        )
    if command in ("init", "upgrade", "uninstall"):
        return None
    manifest = safe_path(root, ".factory/installation.json")
    if not manifest.exists():
        raise FactoryError("Initialize this project first with software-factory init")
    installation = _installation(root)
    if not installation or not isinstance(installation.get("files"), dict):
        raise FactoryError(INVALID_INSTALLATION)
    if installation.get("uninstalled"):
        raise FactoryError(UNINSTALLED_MESSAGE)
    # Refuse to run project runtime source that differs from the installed release, both
    # before exec and when already running it (the .venv entrypoint and uv run skip exec).
    drift = runtime_drift(root, installation)
    if drift:
        raise FactoryError(drift_message(drift))
    local = safe_path(root, ".factory/src/software_factory")
    if _running_pinned(root):
        return None
    # uv environments legitimately contain interpreter symlinks; inspect this one
    # without the managed-file symlink rule used for configuration and evidence.
    python = _runtime_python(root)
    if not python.is_file() or not (local / "cli.py").is_file():
        raise FactoryError(
            "Pinned project runtime is unavailable; run uv sync --locked --no-dev --project .factory "
            "(software-factory upgrade first restores a deleted .factory/src or .factory/run.py)"
        )
    # -B: read-only commands must not write __pycache__ into .factory/src.
    argv = [str(python), "-I", "-B", str(safe_path(root, ".factory/run.py")), *_pinned_arguments(root, raw)]
    if not REPLACE_PROCESS:
        return subprocess.run(check=False, args=argv, cwd=root).returncode
    # Replace this process so signals (SIGTERM, SIGINT) reach the pinned runtime
    # directly; a child would keep running and writing evidence.
    sys.stdout.flush()
    sys.stderr.flush()
    os.chdir(root)
    os.execv(str(python), argv)
    return None  # pragma: no cover - execv does not return


def presented(args, result):
    """What a command prints: mission commands show a summary of the record unless --full is given."""
    if (
        getattr(args, "command", None) == "mission"
        and getattr(args, "mission_command", None) != "status"
        and not getattr(args, "full", False)
    ):
        from .workflow import is_mission_record, mission_summary

        if is_mission_record(result):
            return mission_summary(result)
    return result


def main(argv=None):
    raw = list(sys.argv[1:] if argv is None else argv)
    try:
        cleaned, root_option = _common_options(raw)
        # Dispatch before parsing project command syntax: the pinned runtime may
        # support commands/options unknown to the installed global bootstrap.
        if (
            cleaned
            and not cleaned[0].startswith("-")
            and cleaned[0] not in ("auth", "init", "upgrade", "version")
        ):
            dispatched = _dispatch(resolve_root(root_option), cleaned[0], raw)
            if dispatched is not None:
                raise SystemExit(dispatched)
        parser = build_parser()
        if cleaned and cleaned[0] == "auth":
            from .auth import argument_error

            parser.error = argument_error
        args = parser.parse_args(cleaned)
        target = getattr(args, "path", None)
        if target and root_option and Path(target).resolve() != Path(root_option).resolve():
            raise FactoryError("The target path and --root disagree; select one project")
        args.root = (
            None
            if args.command in ("auth", "version")
            else (
                Path(target or root_option or Path.cwd()).absolute()
                if args.command in ("init", "upgrade")
                else resolve_root(root_option)
            )
        )
        from .events import COMMAND, command_label

        COMMAND.set(command_label(cleaned))
        result = args.handler(args)
        code = result.pop("_exit_code", 0) if isinstance(result, dict) else 0
        result = presented(args, result)
        if result is not None:
            print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
        if code:
            raise SystemExit(code)
    except FactoryError as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(exc.exit_code) from None
    except (OSError, ValueError, subprocess.TimeoutExpired, subprocess.CalledProcessError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from None
    except (KeyError, TypeError, AttributeError) as exc:
        # A malformed record (missing field, wrong type) is a user-facing error, not a traceback.
        message = f"Malformed factory record or input ({type(exc).__name__}: {exc})"
        print(json.dumps({"error": message}, ensure_ascii=False), file=sys.stderr)
        raise SystemExit(1) from None
