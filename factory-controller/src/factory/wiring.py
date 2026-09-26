"""Build a `Factory` (and its adapters) from `Settings` (build spec §3 B7).

This is the ONE place in `factory.pipeline`'s world that imports concrete
adapters. `local` mode wires every port to its `Fake...`/`Recording...`
adapter, so `factory demo` and the test suite never touch the network, real
GitHub, the Anthropic API or Slack. `production` mode wires the real adapters
from `Settings`.

A few production adapters need configuration `Settings` doesn't carry yet
(see the module docstring in `factory-controller/README.md#requests-to-orchestrator`
for the exact fields). Rather than fail hard at startup -- which would make
`factory serve`/`factory worker` unusable even for the parts that *are*
configured -- those adapters are wired to `_NotConfigured`, which raises only
when actually called. The pipeline treats that failure the same way it treats
any other adapter exception: fail closed (a failed holdout run or deploy never
counts as a pass).
"""
from __future__ import annotations

import dataclasses
import logging
import os
from pathlib import Path

from .budget.fake import FakeBudgetGateway
from .clock import SystemClock
from .config import Settings
from .github.app_auth import AppCredentials
from .github.client import RestGitHub
from .github.fake import FakeGitHub
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
    """One `ApproverContact` per owner with a known Slack id.

    `ApproverContact.slack_user_id` is required (`chat.postMessage` DMs need it),
    but `factory.yaml` has no owners-to-Slack-id mapping in the schema yet -- see
    README "Requests to orchestrator": add e.g. `owners_slack: {po: "U0123..."}`.
    Until then no owner has a known id, so the roster is empty: `SlackNotifier`
    still posts the channel-wide FYI (no token, no DM) when a webhook is set.
    """
    fields = {f.name for f in dataclasses.fields(ApproverContact)}
    seen: dict[str, ApproverContact] = {}
    for product in products.all():
        slack_ids = getattr(product, "owner_slack_ids", {})
        for login, roles in product.owner_roles.items():
            slack_user_id = slack_ids.get(login)
            if login in seen or not slack_user_id:
                continue
            kwargs = {"approver": login, "roles": roles, "slack_user_id": slack_user_id}
            if "display_name" in fields:
                kwargs["display_name"] = login
            seen[login] = ApproverContact(**kwargs)
    return list(seen.values())


def _local_adapters(settings: Settings, policy: Policy) -> dict:
    return {
        "github": FakeGitHub(floor=policy.floor),
        "runtime": FakeRuntime(),
        "sandbox": LocalSandbox(repos_root=settings.repos_root, sandbox_root=settings.sandbox_root),
        "budget": FakeBudgetGateway(),
        "notifier": RecordingNotifier(),
        "holdout": FakeHoldoutRunner(result=(1, 1)),
        "deploy": FakeDeployTarget(),
        "flags": FakeFlagProvider(),
    }


def _production_github(settings: Settings, policy: Policy) -> RestGitHub:
    push_creds = AppCredentials(app_id=settings.push_app_id, private_key_path=settings.push_app_private_key_path,
                                installation_id=settings.push_app_installation_id, api_url=settings.github_api_url)
    merge_creds = AppCredentials(app_id=settings.merge_app_id, private_key_path=settings.merge_app_private_key_path,
                                 installation_id=settings.merge_app_installation_id, api_url=settings.github_api_url)
    return RestGitHub(push_credentials=push_creds, merge_credentials=merge_creds, floor=policy.floor,
                      api_url=settings.github_api_url, repos_root=settings.repos_root)


def _production_notifier(settings: Settings, products: ProductRegistry, clock):
    if not settings.slack_bot_token or not settings.inbox_signing_key:
        logger.warning("no slack_bot_token/inbox_signing_key configured; falling back to a logging notifier")
        return RecordingNotifier()
    signer = TokenSigner(settings.inbox_signing_key)
    roster = _build_roster(products)
    return SlackNotifier(bot_token=settings.slack_bot_token, signer=signer, clock=clock, roster=roster,
                         webhook_url=settings.slack_webhook_url)


def _production_holdout(settings: Settings, products: ProductRegistry):
    # WorkflowHoldoutRunner needs its OWN read-only GitHub identity (final draft §11,
    # §13.2) and a single holdout repo/workflow file. `Settings` has neither a holdout
    # token nor a workflow file name yet, and a runner is scoped to one repo, so a
    # multi-product deployment would need one per product. See README "Requests to
    # orchestrator": add FACTORY_HOLDOUT_TOKEN / FACTORY_HOLDOUT_WORKFLOW_FILE (and,
    # for >1 product, a per-product override) to `Settings`.
    return _NotConfigured("HoldoutRunner", "Settings has no holdout runner token/workflow file yet")


def _production_deploy(settings: Settings):
    # CommandDeployTarget needs 5 named commands (build/deploy/health/rollback/url);
    # `Settings.deploy_command` is a single string. See README "Requests to
    # orchestrator": replace it with FACTORY_DEPLOY_{BUILD,DEPLOY,HEALTH,ROLLBACK,URL}_COMMAND.
    if not settings.deploy_command:
        return _NotConfigured("DeployTarget", "Settings.deploy_command is unset")
    return _NotConfigured("DeployTarget", "Settings.deploy_command is a single string; "
                                          "CommandDeployTarget needs 5 named commands")


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
        adapters = _local_adapters(settings, policy)
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
        repos_root=settings.repos_root, gateway_url=settings.llm_gateway_url,
    )


def build_factory_and_adapters(settings: Settings, *, clock=None) -> tuple[Factory, dict, Policy, ProductRegistry]:
    """Like `build_factory`, but also returns the raw adapters, policy and product registry."""
    policy, products = build_policy_and_products(settings)
    adapters = build_adapters(settings, policy, products, clock=clock)
    factory = build_factory(settings, clock=clock, adapters=adapters)
    return factory, adapters, policy, products


__all__ = ["build_adapters", "build_factory", "build_factory_and_adapters", "build_policy_and_products"]
