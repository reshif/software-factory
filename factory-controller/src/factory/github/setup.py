"""Recommended repo settings for a product repo (final draft §13.2).

Builds the branch-protection ruleset and the Actions workflow-permissions
payload described in the security floor, and can either apply them for real
or just print the JSON in a dry run.
"""
import argparse
import json
import logging
from typing import Iterable

import httpx

from ._http import github_headers
from .app_auth import AppCredentials, InstallationTokenProvider

logger = logging.getLogger(__name__)

DEFAULT_REQUIRED_CHECKS = ("lint", "types", "secret_scan", "dep_audit", "unit")


def recommended_ruleset(repo: str, *, branch: str = "main", merge_bot_app_id: int,
                         required_checks: Iterable[str] = DEFAULT_REQUIRED_CHECKS,
                         require_codeowners: bool = True) -> dict:
    """A GitHub ruleset (`POST /repos/{repo}/rulesets`) protecting `branch`.

    - Required status checks and a required, code-owner-reviewed pull request.
    - No deletions, no non-fast-forward pushes.
    - The only bypass actor is the merge-bot GitHub App: nobody else can push
      directly, so every change lands through `merge_pr` (§13.1 #9, §13.2).
    """
    return {
        "name": f"factory-protect-{branch}",
        "target": "branch",
        "enforcement": "active",
        "conditions": {"ref_name": {"include": [f"refs/heads/{branch}"], "exclude": []}},
        "rules": [
            {"type": "deletion"},
            {"type": "non_fast_forward"},
            {
                "type": "pull_request",
                "parameters": {
                    "required_approving_review_count": 1,
                    "require_code_owner_review": require_codeowners,
                    "dismiss_stale_reviews_on_push": True,
                    "require_last_push_approval": False,
                    "required_review_thread_resolution": False,
                },
            },
            {
                "type": "required_status_checks",
                "parameters": {
                    "strict_required_status_checks_policy": True,
                    "do_not_enforce_on_create": False,
                    "required_status_checks": [{"context": name} for name in required_checks],
                },
            },
        ],
        "bypass_actors": [
            {"actor_id": merge_bot_app_id, "actor_type": "Integration", "bypass_mode": "always"},
        ],
    }


def workflow_permissions_payload() -> dict:
    """`PUT /repos/{repo}/actions/permissions/workflow` body.

    Turns off "Allow GitHub Actions to create and approve pull requests" and
    keeps the default token read-only (final draft §13.2).
    """
    return {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": False}


def build_plan(repo: str, *, merge_bot_app_id: int, branch: str = "main",
               required_checks: Iterable[str] = DEFAULT_REQUIRED_CHECKS,
               require_codeowners: bool = True) -> dict:
    return {
        "ruleset": recommended_ruleset(repo, branch=branch, merge_bot_app_id=merge_bot_app_id,
                                       required_checks=required_checks,
                                       require_codeowners=require_codeowners),
        "actions_permissions": workflow_permissions_payload(),
    }


def apply_repo_settings(*, repo: str, merge_bot_app_id: int, token: str | None = None,
                         branch: str = "main", required_checks: Iterable[str] = DEFAULT_REQUIRED_CHECKS,
                         require_codeowners: bool = True, api_url: str = "https://api.github.com",
                         client: httpx.Client | None = None, dry_run: bool = False) -> dict:
    """Builds the recommended repo settings and, unless `dry_run`, applies them.

    Always returns the plan; it never prints anything (that's the caller's
    job — see `main`, which prints it in a dry run).

    `token` must be an installation token with admin rights on `repo`, unless
    `dry_run` is set (in which case no request is made and no token is needed).
    """
    plan = build_plan(repo, merge_bot_app_id=merge_bot_app_id, branch=branch,
                       required_checks=required_checks, require_codeowners=require_codeowners)
    if dry_run:
        return plan

    if not token:
        raise ValueError("a token is required unless dry_run is set")
    headers = github_headers(token)
    owns = client is None
    client = client or httpx.Client(timeout=30.0)
    try:
        resp = client.post(f"{api_url}/repos/{repo}/rulesets", json=plan["ruleset"], headers=headers)
        resp.raise_for_status()
        resp = client.put(f"{api_url}/repos/{repo}/actions/permissions/workflow",
                           json=plan["actions_permissions"], headers=headers)
        resp.raise_for_status()
        logger.info("applied recommended settings to %s", repo)
    finally:
        if owns:
            client.close()
    return plan


def _build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Apply the recommended branch protection ruleset to a factory product repo."
    )
    parser.add_argument("repo", help="owner/name")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--merge-bot-app-id", required=True, type=int)
    parser.add_argument("--check", dest="checks", action="append", default=None,
                        help="a required status check context; repeatable")
    parser.add_argument("--no-codeowners", dest="require_codeowners", action="store_false")
    parser.add_argument("--dry-run", action="store_true", help="print the JSON, make no requests")
    parser.add_argument("--api-url", default="https://api.github.com")
    parser.add_argument("--push-app-id", help="App id to authenticate with (needs admin on the repo)")
    parser.add_argument("--push-private-key-path")
    parser.add_argument("--push-installation-id")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_arg_parser().parse_args(argv)
    checks = tuple(args.checks) if args.checks else DEFAULT_REQUIRED_CHECKS
    token = None
    if not args.dry_run:
        if not (args.push_app_id and args.push_private_key_path and args.push_installation_id):
            raise SystemExit("--push-app-id/--push-private-key-path/--push-installation-id are "
                             "required unless --dry-run is set")
        creds = AppCredentials(app_id=args.push_app_id, private_key_path=args.push_private_key_path,
                               installation_id=args.push_installation_id, api_url=args.api_url)
        token = InstallationTokenProvider(creds).token()
    plan = apply_repo_settings(repo=args.repo, merge_bot_app_id=args.merge_bot_app_id, token=token,
                               branch=args.branch, required_checks=checks,
                               require_codeowners=args.require_codeowners, api_url=args.api_url,
                               dry_run=args.dry_run)
    if args.dry_run:
        print(json.dumps(plan, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
