"""The `factory` command (build spec §3 B7, entry point pinned in `pyproject.toml`)."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from .clock import SystemClock
from .config import Settings
from .inbox.signing import TokenSigner
from .policy import load_policy
from .policy.action_classes import FileChange, classify
from .policy.loader import default_kit_dir


def _settings() -> Settings:
    return Settings.from_env()


# ── factory serve ────────────────────────────────────────────────────────────────
def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .app import create_app

    settings = _settings()
    app = create_app(settings)
    # access_log=False: inbox links carry a signed, single-use approval token in
    # the query string (`GET /inbox/{request_id}?token=...`); uvicorn's default
    # access log would otherwise write that token to stdout/the process log on
    # every request.
    uvicorn.run(app, host=args.host, port=args.port, access_log=False)
    return 0


# ── factory worker ───────────────────────────────────────────────────────────────
def cmd_worker(args: argparse.Namespace) -> int:
    from .wiring import build_factory
    from .worker import Worker

    settings = _settings()
    factory = build_factory(settings)
    Worker(factory).run_forever(interval_s=args.interval)
    return 0


# ── factory migrate ──────────────────────────────────────────────────────────────
def cmd_migrate(args: argparse.Namespace) -> int:
    import psycopg

    from .store.postgres import apply_migrations

    settings = _settings()
    if not settings.database_url:
        print("FACTORY_DATABASE_URL is not set; nothing to migrate (local mode uses an in-memory store)")
        return 0
    conn = psycopg.connect(settings.database_url)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    print("migrations applied")
    return 0


# ── factory validate-kit / factory products validate ────────────────────────────
def cmd_validate_kit(args: argparse.Namespace) -> int:
    try:
        policy = load_policy(args.kit_dir)
    except Exception as exc:  # noqa: BLE001
        print(f"kit policy invalid: {exc}", file=sys.stderr)
        return 1
    print(f"policy_version={policy.policy_version}: OK")
    return 0


def cmd_products_validate(args: argparse.Namespace) -> int:
    from .pipeline.products import ProductRegistry

    settings = _settings()
    kit_dir = Path(args.kit_dir) if args.kit_dir else settings.kit_path
    products_dir = args.products_dir or settings.products_dir
    if not products_dir:
        print("no --products-dir given and FACTORY_PRODUCTS_DIR is unset", file=sys.stderr)
        return 1
    try:
        registry = ProductRegistry(products_dir, kit_dir)
    except Exception as exc:  # noqa: BLE001
        print(f"product config invalid: {exc}", file=sys.stderr)
        return 1
    for product in registry.all():
        print(f"{product.name}: OK (repo={product.repo}, {len(product.standing_mandates)} standing mandate(s))")
    return 0


# ── factory classify ─────────────────────────────────────────────────────────────
def _git_diff_changes(repo_path: str, base: str, head: str) -> list[FileChange]:
    def run(*args: str) -> str:
        return subprocess.run(["git", "-C", repo_path, *args], capture_output=True, text=True, check=True).stdout

    name_status = {}
    for line in run("diff", "--no-renames", "--name-status", base, head).splitlines():
        if not line.strip():
            continue
        code, path = line.split("\t", 1)
        name_status[path] = {"A": "added", "M": "modified", "D": "deleted"}.get(code[0], "modified")
    numstat = {}
    for line in run("diff", "--no-renames", "--numstat", base, head).splitlines():
        if not line.strip():
            continue
        added, removed, path = line.split("\t", 2)
        numstat[path] = (0 if added == "-" else int(added), 0 if removed == "-" else int(removed))
    changes = []
    for path, status in name_status.items():
        added, removed = numstat.get(path, (0, 0))
        changes.append(FileChange(path=path, status=status, added_lines=added, removed_lines=removed))
    return changes


def cmd_classify(args: argparse.Namespace) -> int:
    base, _, head = args.git.partition("..")
    if not base or not head:
        print("--git must look like BASE..HEAD", file=sys.stderr)
        return 1
    repo_path = args.repo or "."
    policy = load_policy(args.kit_dir)
    changes = _git_diff_changes(repo_path, base, head)
    if not changes:
        print("no changes between the given refs", file=sys.stderr)
        return 1
    result = classify(changes, policy.floor)
    print(json.dumps({"action_class": result.action_class, "reasons": list(result.reasons),
                      "protected_touched": list(result.protected_touched),
                      "forbidden_touched": list(result.forbidden_touched),
                      "diff_lines": result.diff_lines}, indent=2))
    return 0


# ── factory gates ────────────────────────────────────────────────────────────────
def cmd_gates(args: argparse.Namespace) -> int:
    from .controller.gate_resolver import resolve
    from .policy import PolicyError

    policy = load_policy(args.kit_dir)
    try:
        decision = resolve(policy, action_class=args.action_class, risk_profile=args.profile,
                           autonomy_level=args.level, protected_touched=args.protected)
    except PolicyError as exc:
        # An unknown --class/--profile/--level is a usage error, not a bug: report
        # it the way argparse itself would (a clean message, exit code 2), not as
        # an uncaught traceback. --profile/--level already have argparse `choices=`
        # and exit 2 automatically; --class has no fixed choice set (it's the
        # kit's own policy data), so this is the analogous check for it.
        print(f"factory gates: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({"h1": str(decision.h1), "hm": str(decision.hm), "h2": str(decision.h2),
                      "rules": list(decision.rules)}, indent=2))
    return 0


# ── factory inbox-link ───────────────────────────────────────────────────────────
def cmd_inbox_link(args: argparse.Namespace) -> int:
    settings = _settings()
    if not settings.inbox_signing_key:
        print("FACTORY_INBOX_SIGNING_KEY is not set", file=sys.stderr)
        return 1
    roles = tuple(r.strip() for r in args.roles.split(",") if r.strip())
    signer = TokenSigner(settings.inbox_signing_key)
    expires = None
    if settings.database_url or args.request:
        try:
            from .store import open_store
            store = open_store(settings)
            request = store.approvals.get(args.request)
            expires = request.expires
        except Exception:  # noqa: BLE001 -- fall back to a short-lived link below
            expires = None
    if expires is None:
        from datetime import timedelta
        expires = SystemClock().now() + timedelta(days=2)
    token = signer.issue(approver=args.approver, roles=roles, expires=expires, request_id=args.request)
    print(f"{settings.inbox_base_url.rstrip('/')}/inbox/{args.request}?token={token}")
    return 0


# ── factory kill-switch / resume / unblock ───────────────────────────────────────
def cmd_kill_switch(args: argparse.Namespace) -> int:
    """Emergency stop: every non-terminal mission moves to HELD (no worker acts on
    a HELD mission until `factory resume` brings it back)."""
    from .wiring import build_factory

    factory = build_factory(_settings())
    factory.kill_switch()
    print("kill switch engaged: every non-terminal mission moved to HELD")
    return 0


def cmd_resume(args: argparse.Namespace) -> int:
    """Resume one HELD mission (after `factory kill-switch`, or a side-state
    timeout that fired before a human could act)."""
    from .wiring import build_factory

    factory = build_factory(_settings())
    factory.resume(args.mission)
    print(f"{args.mission}: resumed")
    return 0


def cmd_unblock(args: argparse.Namespace) -> int:
    """Clear one BLOCKED mission so the worker picks it back up."""
    from .wiring import build_factory

    factory = build_factory(_settings())
    factory.unblock(args.mission)
    print(f"{args.mission}: unblocked")
    return 0


# ── factory github setup ─────────────────────────────────────────────────────────
def cmd_github_setup(args: argparse.Namespace) -> int:
    """Apply (or, with --dry-run, just print) the recommended branch-protection
    ruleset for one product repo -- required checks, required CODEOWNERS review,
    and a merge restriction to the merge-bot App (final draft §13.2)."""
    from .github import setup as github_setup
    from .github.app_auth import AppCredentials, InstallationTokenProvider

    settings = _settings()
    if not settings.merge_app_id:
        print("factory github setup: FACTORY_MERGE_APP_ID is not set", file=sys.stderr)
        return 1
    try:
        merge_bot_app_id = int(settings.merge_app_id)
    except ValueError:
        print(f"factory github setup: FACTORY_MERGE_APP_ID must be numeric, got "
              f"{settings.merge_app_id!r}", file=sys.stderr)
        return 1

    token = None
    if not args.dry_run:
        missing = [name for name, value in (
            ("FACTORY_PUSH_APP_ID", settings.push_app_id),
            ("FACTORY_PUSH_APP_PRIVATE_KEY_PATH", settings.push_app_private_key_path),
            ("FACTORY_PUSH_APP_INSTALLATION_ID", settings.push_app_installation_id),
        ) if not value]
        if missing:
            print(f"factory github setup (without --dry-run) needs {', '.join(missing)}", file=sys.stderr)
            return 1
        creds = AppCredentials(app_id=settings.push_app_id, private_key_path=settings.push_app_private_key_path,
                               installation_id=settings.push_app_installation_id, api_url=settings.github_api_url)
        token = InstallationTokenProvider(creds).token()

    plan = github_setup.apply_repo_settings(repo=args.repo, merge_bot_app_id=merge_bot_app_id, token=token,
                                            api_url=settings.github_api_url, dry_run=args.dry_run)
    print(json.dumps(plan, indent=2, sort_keys=True))
    return 0


# ── factory demo ─────────────────────────────────────────────────────────────────
def cmd_demo(args: argparse.Namespace) -> int:
    from .demo.runner import run_all, run_scenario, serve_demo

    if args.serve:
        serve_demo(host=args.host, port=args.port)
        return 0
    if args.scenario == "all":
        return 0 if run_all() else 1
    return 0 if run_scenario(args.scenario) else 1


_KIT_DIR_HELP = "path to factory-kit (default: ../factory-kit next to this package, or FACTORY_KIT_DIR)"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="factory", description="The software factory controller.")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("serve", help="run the FastAPI app (webhooks, inbox, health, metrics)")
    p.add_argument("--host", default="0.0.0.0", help="interface to bind (default: 0.0.0.0)")
    p.add_argument("--port", type=int, default=8080, help="port to bind (default: 8080)")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("worker", help="run the background worker loop")
    p.add_argument("--interval", type=float, default=5.0, help="seconds to sleep between ticks (default: 5.0)")
    p.set_defaults(func=cmd_worker)

    p = sub.add_parser("migrate", help="apply Postgres migrations (no-op in local/in-memory mode)")
    p.set_defaults(func=cmd_migrate)

    p = sub.add_parser("validate-kit", help="validate the factory-kit policies")
    p.add_argument("--kit-dir", default=None, help=_KIT_DIR_HELP)
    p.set_defaults(func=cmd_validate_kit)

    products = sub.add_parser("products", help="product-cell config commands")
    products_sub = products.add_subparsers(dest="products_command", required=True)
    p = products_sub.add_parser("validate", help="validate every <product>/factory.yaml + mandates")
    p.add_argument("--products-dir", default=None,
                   help="directory of <product>/factory.yaml (default: FACTORY_PRODUCTS_DIR)")
    p.add_argument("--kit-dir", default=None, help=_KIT_DIR_HELP)
    p.set_defaults(func=cmd_products_validate)

    p = sub.add_parser("classify", help="classify a git diff's action class")
    p.add_argument("--git", required=True, help="BASE..HEAD")
    p.add_argument("--repo", default=None, help="path to the git repo (default: cwd)")
    p.add_argument("--kit-dir", default=None, help=_KIT_DIR_HELP)
    p.set_defaults(func=cmd_classify)

    p = sub.add_parser("gates", help="resolve the gate table for one action class")
    p.add_argument("--class", dest="action_class", required=True, help="action class, e.g. AC4 (see the kit policy)")
    p.add_argument("--profile", required=True, choices=["experimental", "standard", "regulated"],
                   help="the product's risk profile")
    p.add_argument("--level", default="L3", choices=["L3", "L4", "L5"],
                   help="autonomy level to resolve at (default: L3)")
    p.add_argument("--protected", action="store_true", help="the diff touches a protected path")
    p.add_argument("--kit-dir", default=None, help=_KIT_DIR_HELP)
    p.set_defaults(func=cmd_gates)

    p = sub.add_parser("inbox-link", help="mint a signed, request-bound inbox link")
    p.add_argument("--request", required=True, help="the approval request id")
    p.add_argument("--approver", required=True, help="the approver's login, e.g. @tl")
    p.add_argument("--roles", required=True, help="comma-separated, e.g. tech_lead,security")
    p.set_defaults(func=cmd_inbox_link)

    p = sub.add_parser("kill-switch",
                       help="emergency stop: move every non-terminal mission to HELD")
    p.set_defaults(func=cmd_kill_switch)

    p = sub.add_parser("resume", help="resume one HELD mission")
    p.add_argument("--mission", required=True, help="mission id to resume, e.g. MIS-42")
    p.set_defaults(func=cmd_resume)

    p = sub.add_parser("unblock", help="clear one BLOCKED mission so the worker picks it back up")
    p.add_argument("--mission", required=True, help="mission id to unblock, e.g. MIS-42")
    p.set_defaults(func=cmd_unblock)

    github = sub.add_parser("github", help="GitHub repo setup commands")
    github_sub = github.add_subparsers(dest="github_command", required=True)
    p = github_sub.add_parser("setup", help="apply the recommended branch protection ruleset to a product repo")
    p.add_argument("--repo", required=True, help="owner/name of the repo to configure")
    p.add_argument("--dry-run", action="store_true", help="print the plan as JSON; make no GitHub API calls")
    p.set_defaults(func=cmd_github_setup)

    from .demo.scenarios import SCENARIOS
    scenario_choices = ["all", *sorted(SCENARIOS)]
    p = sub.add_parser("demo", help="run scripted end-to-end demo scenarios on fakes")
    p.add_argument("--scenario", default="all", choices=scenario_choices,
                   help="scenario to run, or 'all' (default: all)")
    p.add_argument("--serve", action="store_true", help="start the app + worker with fakes for browser approvals")
    p.add_argument("--host", default="127.0.0.1", help="interface to bind with --serve (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8080, help="port to bind with --serve (default: 8080)")
    p.set_defaults(func=cmd_demo)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
