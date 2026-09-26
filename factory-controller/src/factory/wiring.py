"""Build a `Factory` (and its adapters) from `Settings` (build spec §3 B7).

This is the ONE place in `factory.pipeline`'s world that imports concrete
adapters. `local` mode wires every port to its `Fake...`/`Recording...`
adapter, so `factory demo` and the test suite never touch the network, real
GitHub, the Anthropic API or Slack. `production` mode wires the real adapters
from `Settings`.

Production `DeployTarget` fails FAST at startup if any of its 5 hook commands
is missing (a mis-configured release path is a deploy-time bug you want to
catch before the first mission ever reaches H2, not the first time it does).
`HoldoutRunner` does the same only when some product's `verification.required`
actually names `holdout_blackbox` -- a product cell that doesn't require it
can still run without a holdout token configured, and gets `_NotConfigured`
(raises only if something calls it anyway), same as before. See
`factory-controller/README.md#requests-to-orchestrator` for what's still
outstanding (a `RepoMirror` production identity is the push bot's, reused
read-only; per-product holdout repos are supported, a single product is the
common case).
"""
from __future__ import annotations

import dataclasses
import logging
import os
from pathlib import Path

from .budget.fake import FakeBudgetGateway
from .clock import SystemClock
from .config import Settings
from .github.app_auth import AppCredentials, InstallationTokenProvider
from .github.client import RestGitHub, default_remote_url
from .github.fake import FakeGitHub
from .github.mirror import FakeMirror, GitMirror, mirror_path
from .inbox.fake import RecordingNotifier
from .inbox.signing import TokenSigner
from .inbox.slack import ApproverContact, SlackNotifier
from .pipeline.orchestrator import Factory
from .pipeline.products import ProductRegistry
from .policy import Policy, load_policy
from .release.deploy import CommandDeployTarget, FakeDeployTarget
from .release.flags import FakeFlagProvider, FileFlagProvider
from .runtime.claude import ClaudeRuntime
from .runtime.fake import FakeRuntime
from .sandbox.docker import DockerSandbox
from .sandbox.local import LocalSandbox
from .store.memory import MemoryStateStore
from .store.postgres import PostgresStateStore, apply_migrations
from .verification.holdout import FakeHoldoutRunner, WorkflowHoldoutRunner

logger = logging.getLogger(__name__)


def _local_mirror(settings: Settings, products: ProductRegistry) -> FakeMirror:
    """`FakeMirror`, pre-registered for every product's repo at the same path
    convention `SandboxPort.create` clones from (`mirror_path`). The demo
    harness creates each repo's checkout directly there, so `sync` never
    actually needs to fetch anything.
    """
    mirror = FakeMirror()
    for product in products.all():
        if product.repo:
            mirror.register(product.repo, mirror_path(settings.repos_root, product.repo))
    return mirror


class _NotConfigured:
    """A port stand-in that raises only when actually used (see module docstring)."""

    def __init__(self, port_name: str, reason: str):
        self._port_name = port_name
        self._reason = reason

    def __getattr__(self, name):
        def _raise(*args, **kwargs):
            raise RuntimeError(f"{self._port_name}.{name}: not configured in production ({self._reason})")
        return _raise


def build_policy_and_products(settings: Settings) -> tuple[Policy, ProductRegistry]:
    policy = load_policy(settings.kit_path)
    products_dir = settings.products_dir or str(settings.kit_path.parent / "products")
    products = ProductRegistry(products_dir, settings.kit_path)
    return policy, products


def _build_roster(products: ProductRegistry) -> list[ApproverContact]:
    """One `ApproverContact` per owner with a `factory.yaml` `slack_ids` entry.

    An owner with no `slack_ids` mapping gets no DM (only the channel-wide FYI,
    if a webhook is configured) -- `factory.yaml` is the source of truth for
    who's reachable on Slack, not a guess.
    """
    fields = {f.name for f in dataclasses.fields(ApproverContact)}
    seen: dict[str, ApproverContact] = {}
    for product in products.all():
        for login, roles in product.owner_roles.items():
            slack_user_id = product.slack_ids.get(login)
            if login in seen or not slack_user_id:
                continue
            kwargs = {"approver": login, "roles": roles, "slack_user_id": slack_user_id}
            if "display_name" in fields:
                kwargs["display_name"] = login
            seen[login] = ApproverContact(**kwargs)
    return list(seen.values())


def _local_adapters(settings: Settings, policy: Policy, products: ProductRegistry) -> dict:
    return {
        "github": FakeGitHub(floor=policy.floor),
        "runtime": FakeRuntime(),
        "sandbox": LocalSandbox(repos_root=settings.repos_root, sandbox_root=settings.sandbox_root),
        "budget": FakeBudgetGateway(),
        "notifier": RecordingNotifier(),
        "holdout": FakeHoldoutRunner(result=(1, 1)),
        "deploy": FakeDeployTarget(),
        "flags": FakeFlagProvider(),
        "mirror": _local_mirror(settings, products),
    }


def _production_github(settings: Settings, policy: Policy) -> RestGitHub:
    push_creds = AppCredentials(app_id=settings.push_app_id, private_key_path=settings.push_app_private_key_path,
                                installation_id=settings.push_app_installation_id, api_url=settings.github_api_url)
    merge_creds = AppCredentials(app_id=settings.merge_app_id, private_key_path=settings.merge_app_private_key_path,
                                 installation_id=settings.merge_app_installation_id, api_url=settings.github_api_url)
    return RestGitHub(push_credentials=push_creds, merge_credentials=merge_creds, floor=policy.floor,
                      api_url=settings.github_api_url, repos_root=settings.repos_root)


def _production_mirror(settings: Settings) -> GitMirror:
    # The mirror only ever reads (clones/fetches), never pushes (§13.1 #5), so the
    # push bot's identity -- already read-scoped for issues/PRs -- is reused here
    # rather than minting a third GitHub App identity for it.
    push_creds = AppCredentials(app_id=settings.push_app_id, private_key_path=settings.push_app_private_key_path,
                                installation_id=settings.push_app_installation_id, api_url=settings.github_api_url)
    token_provider = InstallationTokenProvider(push_creds)
    return GitMirror(repos_root=settings.repos_root, token_provider=token_provider,
                     remote_url_builder=default_remote_url)


def _production_notifier(settings: Settings, products: ProductRegistry, clock):
    if not settings.slack_bot_token:
        logger.warning("FACTORY_SLACK_BOT_TOKEN is unset; falling back to a logging notifier")
        return RecordingNotifier()
    signer = TokenSigner(settings.inbox_signing_key)
    roster = _build_roster(products)
    return SlackNotifier(bot_token=settings.slack_bot_token, signer=signer, clock=clock, roster=roster,
                         webhook_url=settings.slack_webhook_url)


class _MultiProductHoldoutRunner:
    """Dispatches to one `WorkflowHoldoutRunner` per product's `holdout_repo`
    (a runner is scoped to a single repo; a product cell may have its own).
    """

    def __init__(self, *, token: str, workflow: str, api_url: str, products: ProductRegistry):
        self._runners: dict[str, WorkflowHoldoutRunner] = {}
        for product in products.all():
            if product.holdout_repo:
                self._runners[product.name] = WorkflowHoldoutRunner(
                    api_url=api_url, repo=product.holdout_repo, workflow_file=workflow, token=token)

    def run(self, product: str, *, staging_url: str, artifact: str):
        runner = self._runners.get(product)
        if runner is None:
            raise RuntimeError(f"HoldoutRunner: product {product!r} has no holdout_repo configured")
        return runner.run(product, staging_url=staging_url, artifact=artifact)


def _products_requiring_holdouts(products: ProductRegistry) -> list[str]:
    return [p.name for p in products.all() if "holdout_blackbox" in p.verification_required]


def _production_holdout(settings: Settings, products: ProductRegistry) -> _MultiProductHoldoutRunner | _NotConfigured:
    needing = _products_requiring_holdouts(products)
    if needing and not settings.holdout_token:
        raise RuntimeError(
            f"product(s) {needing} require the 'holdout_blackbox' check but "
            f"FACTORY_HOLDOUT_TOKEN is unset -- the holdout runner needs its own "
            f"read-only identity (final draft §13.1 #4) before it can run")
    if not settings.holdout_token:
        return _NotConfigured("HoldoutRunner", "FACTORY_HOLDOUT_TOKEN is unset")
    return _MultiProductHoldoutRunner(token=settings.holdout_token, workflow=settings.holdout_workflow,
                                      api_url=settings.github_api_url, products=products)


_DEPLOY_HOOKS = {"build": "deploy_build_cmd", "deploy": "deploy_cmd", "health": "deploy_health_cmd",
                 "rollback": "deploy_rollback_cmd", "url": "deploy_url_cmd"}


def _production_deploy(settings: Settings) -> CommandDeployTarget:
    missing = [f"FACTORY_{field.upper()}" for field in _DEPLOY_HOOKS.values() if not getattr(settings, field)]
    if missing:
        raise RuntimeError(f"production DeployTarget needs all 5 deploy hooks; missing: {', '.join(missing)}")
    commands = {name: getattr(settings, field) for name, field in _DEPLOY_HOOKS.items()}
    return CommandDeployTarget(commands=commands, fencing_state_path=os.path.join(settings.deploy_state_dir,
                                                                                  "fencing.json"))


def _production_flags(settings: Settings) -> FileFlagProvider:
    return FileFlagProvider(os.path.join(settings.evidence_dir, "flags.json"))


def _open_store(settings: Settings, clock) -> MemoryStateStore | PostgresStateStore:
    """Like `store.open_store(settings)`, but threads `clock` through so the store's
    own `updated_at`/event timestamps agree with the orchestrator's (real time in
    production, `FakeClock` in the demo/tests -- see README "v1 scope notes").
    """
    if not settings.database_url:
        return MemoryStateStore(clock=clock)
    import psycopg
    conn = psycopg.connect(settings.database_url)
    try:
        apply_migrations(conn)
    finally:
        conn.close()
    return PostgresStateStore(settings.database_url, clock=clock)


def build_adapters(settings: Settings, policy: Policy, products: ProductRegistry, *, clock=None) -> dict:
    """Build every adapter `Factory` needs, keyed by its constructor argument name."""
    clock = clock or SystemClock()
    if settings.mode == "local":
        adapters = _local_adapters(settings, policy, products)
    else:
        adapters = {
            "github": _production_github(settings, policy),
            "runtime": ClaudeRuntime(cli_path=settings.claude_cli_path),
            "sandbox": DockerSandbox(image=settings.sandbox_image, repos_root=settings.repos_root,
                                     sandbox_root=settings.sandbox_root,
                                     egress_proxy_url=settings.egress_proxy_url),
            "budget": _budget_gateway(settings),
            "notifier": _production_notifier(settings, products, clock),
            "holdout": _production_holdout(settings, products),
            "deploy": _production_deploy(settings),
            "flags": _production_flags(settings),
            "mirror": _production_mirror(settings),
        }
    adapters["store"] = _open_store(settings, clock)
    return adapters


def _budget_gateway(settings: Settings):
    from .budget.litellm import LiteLLMGateway
    return LiteLLMGateway(base_url=settings.llm_gateway_url, master_key=settings.llm_gateway_master_key)


def build_factory(settings: Settings, *, clock=None, adapters: dict | None = None) -> Factory:
    """Build the fully-wired `Factory` for `settings`.

    `adapters`, when given, is used in place of freshly built ones (the demo
    and tests pass their own so they can keep a reference to the concrete
    fakes -- e.g. to call `FakeGitHub.add_issue` -- while the `Factory` only
    ever sees them through the ports it depends on).
    """
    policy, products = build_policy_and_products(settings)
    built = adapters if adapters is not None else build_adapters(settings, policy, products, clock=clock)
    return Factory(
        store=built["store"], github=built["github"], runtime=built["runtime"], sandbox=built["sandbox"],
        budget=built["budget"], notifier=built["notifier"], holdout=built["holdout"], deploy=built["deploy"],
        flags=built["flags"], products=products, policy=policy, clock=clock or SystemClock(),
        kit_dir=settings.kit_path, inbox_base_url=settings.inbox_base_url, evidence_dir=settings.evidence_dir,
        repos_root=settings.repos_root, mirror=built["mirror"], gateway_url=settings.llm_gateway_url,
        state_encryption_key=settings.state_encryption_key,
    )


def build_factory_and_adapters(settings: Settings, *, clock=None) -> tuple[Factory, dict, Policy, ProductRegistry]:
    """Like `build_factory`, but also returns the raw adapters, policy and product registry."""
    policy, products = build_policy_and_products(settings)
    adapters = build_adapters(settings, policy, products, clock=clock)
    factory = build_factory(settings, clock=clock, adapters=adapters)
    return factory, adapters, policy, products


__all__ = ["build_adapters", "build_factory", "build_factory_and_adapters", "build_policy_and_products"]
