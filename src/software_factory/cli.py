"""Command entrypoint and pinned project-runtime dispatch."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import __version__
from .core import FactoryError, git, load_config, resolve_root, runtime_fingerprint, safe_path, sha256

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


def inspect_project(root: Path) -> dict:
    suggestions = []
    package = root / "package.json"
    if package.is_file():
        try:
            scripts = json.loads(package.read_text()).get("scripts", {})
            for name in ("test", "lint", "typecheck", "build"):
                if name in scripts:
                    suggestions.append({"id": name, "command": ["npm", "run", name], "cwd": "."})
        except (ValueError, OSError):
            pass
    if (root / "pyproject.toml").is_file():
        suggestions.append({"id": "python-tests", "command": ["uv", "run", "pytest"], "cwd": "."})
    if (root / "go.mod").is_file():
        suggestions.append({"id": "go-tests", "command": ["go", "test", "./..."], "cwd": "."})
    if (root / "Cargo.toml").is_file():
        suggestions.append({"id": "rust-tests", "command": ["cargo", "test"], "cwd": "."})
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
    from .installation import jev_summary
    from .rendering import render
    from .transactions import JOURNAL

    report = inspect_project(root)
    issues = []
    installation = _installation(root)
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
    try:
        config = load_config(root)
        report["profiles"] = config["profile"]
        report["jev"] = {
            **jev_summary(config),
            "credential": "not_checked",
            "network": "not_checked",
        }
        if skew or uninstalled:
            report["exports"] = "not_checked"
        else:
            report["exports"] = render(root, check=True)
            entry_skills = report["exports"]["relinquished_entry_skills"]
            replaced = sorted(n for n, state in entry_skills.items() if state == "replaced")
            missing = sorted(n for n, state in entry_skills.items() if state == "missing")
            if replaced:
                issues.append(
                    {
                        "severity": "error",
                        "code": "entry_skill_replaced",
                        "message": "Factory entry-point skills (factory-build, factory-blueprint, "
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
        if any(c["id"] == "configure-me" for c in config["checks"]):
            issues.append(
                {
                    "severity": "warning",
                    "code": "configure_checks",
                    "message": "Replace the placeholder with required product checks in factory.json",
                }
            )
        for owner in ("maintainer", "reviewer"):
            if not config.get("owners", {}).get(owner):
                issues.append(
                    {
                        "severity": "warning",
                        "code": "owner_unconfigured",
                        "message": f"Set owners.{owner} before relying on team governance",
                    }
                )
    except (FactoryError, OSError) as exc:
        issues.append({"severity": "error", "code": "configuration", "message": str(exc)})
    if installation and not uninstalled:
        drift = runtime_drift(root, installation)
        if drift:
            issues.append(
                {
                    "severity": "error",
                    "code": "runtime_drift",
                    "message": "Project runtime source differs from the installed release: "
                    + ", ".join(drift[:10])
                    + ("" if len(drift) <= 10 else f" and {len(drift) - 10} more")
                    + ". Restore it from Git or run software-factory upgrade",
                }
            )
    if not uninstalled and not local_python.is_file():
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
        ("inspect", "Inspect a project and suggest checks without changing files"),
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
    result, root = [], None
    iterator = iter(argv)
    for token in iterator:
        if token == "--root":
            if root is not None:
                raise FactoryError("Duplicate --root")
            root = next(iterator, None)
            if not root or root.startswith("--"):
                raise FactoryError("--root requires a path")
        elif token.startswith("--root="):
            if root is not None:
                raise FactoryError("Duplicate --root")
            root = token.split("=", 1)[1]
        else:
            result.append(token)
    return result, root


def _pinned_arguments(root: Path, raw: list[str]) -> list[str]:
    """Pin --root to the resolved project; the pinned process starts in that root."""
    result, iterator = [], iter(raw)
    for token in iterator:
        if token == "--root":
            next(iterator, None)
            result.append("--root=" + str(root))
        elif token.startswith("--root="):
            result.append("--root=" + str(root))
        else:
            result.append(token)
    return result


def _dispatch(root: Path, command: str, raw: list[str]) -> int | None:
    from .transactions import JOURNAL

    helping = any(a in raw for a in ("--help", "-h"))
    if command in ("auth", "recover", "version") or helping:
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
        if command not in ("doctor", "inspect"):
            raise FactoryError("Initialize this project first with software-factory init")
        return None
    installation = _installation(root)
    if installation and installation.get("uninstalled"):
        if command in ("doctor", "inspect"):
            return None
        raise FactoryError(UNINSTALLED_MESSAGE)
    local = safe_path(root, ".factory/src/software_factory")
    if _running_pinned(root):
        return None
    # uv environments legitimately contain interpreter symlinks; inspect this one
    # without the managed-file symlink rule used for configuration and evidence.
    python = _runtime_python(root)
    if not python.is_file() or not (local / "cli.py").is_file():
        if command in ("doctor", "inspect"):
            return None
        raise FactoryError(
            "Pinned project runtime is unavailable; run uv sync --locked --no-dev --project .factory "
            "(or software-factory upgrade if .factory/src is missing)"
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


def main(argv=None):
    raw = list(sys.argv[1:] if argv is None else argv)
    try:
        cleaned, root_option = _common_options(raw)
        # Dispatch before parsing project command syntax: the pinned runtime may
        # support commands/options unknown to the installed global bootstrap.
        if cleaned and not cleaned[0].startswith("-") and cleaned[0] not in ("auth", "init", "upgrade"):
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
            if args.command == "auth"
            else (
                Path(target or root_option or Path.cwd()).absolute()
                if args.command in ("init", "upgrade")
                else resolve_root(root_option)
            )
        )
        result = args.handler(args)
        code = result.pop("_exit_code", 0) if isinstance(result, dict) else 0
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
