"""Demo harness: a temporary product repo + fully-wired local `Factory` (build spec §3 B7).

Every scenario in `demo/scenarios.py` gets a fresh `DemoContext`: a copy of the
kit's `templates/backend-service` sample app, git-initialized as its own repo
mirror, registered as a product, and wired to `Factory` with every port on its
`Fake.../Recording...` adapter (`wiring.build_factory_and_adapters`, `local`
mode) -- no network, no real GitHub, no Anthropic API, no Slack.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml

from ..clock import FakeClock
from ..config import Settings
from ..controller.approvals import ApprovalRequest
from ..github.webhooks import CheckSuiteCompleted, IssueLabeled
from ..models import CheckResult, MissionRecord, WorkItem
from ..pipeline.orchestrator import Factory
from ..pipeline.products import Product, ProductRegistry
from ..policy import Policy
from ..policy.loader import default_kit_dir
from ..wiring import build_factory_and_adapters

REPO = "org/backend-service"
PRODUCT_NAME = "backend-service"


def _git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "demo", "GIT_AUTHOR_EMAIL": "demo@example.com",
           "GIT_COMMITTER_NAME": "demo", "GIT_COMMITTER_EMAIL": "demo@example.com"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True,
                          env=env).stdout


def _init_mirror(product_dir: Path) -> str:
    _git(product_dir, "init", "--quiet", "--initial-branch=main")
    _git(product_dir, "add", "-A")
    _git(product_dir, "commit", "--quiet", "-m", "initial: backend-service sample app")
    return _git(product_dir, "rev-parse", "HEAD").strip()


def _patch_factory_yaml(product_dir: Path, *, risk_profile: str | None = None,
                        autonomy_level: str | None = None) -> None:
    """Extend the demo's OWN COPY of `factory.yaml` to require `review_agent` and
    `holdout_blackbox` too, so the demo exercises the full HM/H2 verification
    layers the final draft describes (§9.2). This never touches the checked-in
    kit template -- only the temp copy this harness made for the run.

    `risk_profile`, when given, overrides the template's `standard` profile --
    used by `tests/pipeline` to exercise the `experimental`-only "auto" HM gate
    (final draft §6.2), which the standard-profile demo scenarios never reach.
    `autonomy_level` similarly overrides `L3`, e.g. to reach L5's "standing" H2
    relaxation for AC1-3 (autonomy-levels.yaml), which no L3 demo scenario reaches.
    """
    path = product_dir / "factory.yaml"
    doc = yaml.safe_load(path.read_text())
    required = list(doc["verification"]["required"])
    for name in ("review_agent", "holdout_blackbox"):
        if name not in required:
            required.append(name)
    doc["verification"]["required"] = required
    if risk_profile:
        doc["risk_profile"] = risk_profile
    if autonomy_level:
        doc["autonomy_level"] = autonomy_level
    path.write_text(yaml.safe_dump(doc, sort_keys=False))


@dataclass
class DemoContext:
    tmp: Path
    settings: Settings
    clock: FakeClock
    factory: Factory
    adapters: dict
    policy: Policy
    products: ProductRegistry
    product: Product
    repo: str
    base_commit: str
    _next_issue: int = 1

    def next_issue_number(self) -> int:
        self._next_issue += 1
        return self._next_issue - 1

    def add_work_item(self, *, title: str, body: str = "", labels=("factory:feature",),
                      author: str = "@dev") -> WorkItem:
        number = self.next_issue_number()
        item = WorkItem(item_id=f"{self.repo}#{number}", product=self.product.name, repo=self.repo,
                        number=number, title=title, body=body, labels=tuple(labels), author=author)
        self.adapters["github"].add_issue(item)
        return item

    def start(self, item: WorkItem) -> MissionRecord:
        event = IssueLabeled(repo=item.repo, number=item.number, label=item.labels[0], title=item.title,
                             body=item.body, author=item.author)
        mission = self.factory.on_issue_labeled(event)
        assert mission is not None, "on_issue_labeled did not start a mission"
        return mission

    def mission(self, mission_id: str) -> MissionRecord:
        return self.factory.store.get_mission(mission_id)

    def open_request(self, mission_id: str, gate: str) -> ApprovalRequest:
        matches = [r for r in self.factory.store.approvals.list_open(mission_id) if r.gate == gate]
        assert matches, f"no open {gate} request for {mission_id}"
        return matches[-1]

    def decide(self, mission_id: str, gate: str, decision: str, *, approver: str | None = None) -> None:
        """Record a decision and immediately process it (`Factory.decide` only
        enqueues; a running factory has a `Worker` calling `process_approvals`
        on every tick -- the demo simulates that happening right away)."""
        req = self.open_request(mission_id, gate)
        approver = approver or self.product.approvers_for(gate)[0]
        roles = self.product.roles_for(approver)
        self.factory.decide(req.request_id, approver, roles, decision, req.content_hash)
        self.factory.process_approvals()

    def pending_auto_merge_sha(self, mission_id: str) -> str:
        cached = self.factory.recall(mission_id, "pending_auto_merge")
        assert cached, f"{mission_id}: no pending auto-merge sha recorded"
        return cached["sha"]

    def merge_sha(self, mission_id: str) -> str:
        cached = self.factory.recall(mission_id, "merge_sha")
        assert cached, f"{mission_id}: no merge sha recorded"
        return cached["sha"]

    def pass_checks(self, sha: str, names: tuple = ("lint", "unit")) -> None:
        self.adapters["github"].set_checks(self.repo, sha, [CheckResult(n, "success") for n in names])

    def fail_check(self, sha: str, *, failing: str, others: tuple = ("lint", "unit")) -> None:
        checks = [CheckResult(n, "success" if n != failing else "failure") for n in others]
        self.adapters["github"].set_checks(self.repo, sha, checks)

    def fire_check_suite(self, sha: str, *, conclusion: str = "success") -> None:
        self.factory.on_check_suite_completed(CheckSuiteCompleted(repo=self.repo, sha=sha, conclusion=conclusion))

    def events(self, mission_id: str) -> list[dict]:
        return [e for e in self.factory.store.list_events(mission_id) if not str(e["kind"]).startswith("_cache.")]

    def print_timeline(self, mission_id: str, *, title: str) -> None:
        print(f"\n=== {title} ({mission_id}) ===")
        for event in self.events(mission_id):
            at = event["at"].isoformat() if hasattr(event["at"], "isoformat") else event["at"]
            print(f"  {at}  {event['kind']:<24} {event.get('payload', {})}")
        print(f"  final state: {self.mission(mission_id).state}")

    def cleanup(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)


def new_context(*, holdout_result: tuple = (2, 2), clock=None, risk_profile: str | None = None,
                autonomy_level: str | None = None) -> DemoContext:
    tmp = Path(tempfile.mkdtemp(prefix="factory-demo-"))
    kit_dir = default_kit_dir()
    products_dir = tmp / "products"
    product_dir = products_dir / "org__backend-service"
    shutil.copytree(kit_dir / "templates" / "backend-service", product_dir)
    _patch_factory_yaml(product_dir, risk_profile=risk_profile, autonomy_level=autonomy_level)
    base_commit = _init_mirror(product_dir)

    settings = Settings(
        mode="local", kit_dir=str(kit_dir), products_dir=str(products_dir),
        inbox_base_url="http://localhost:8080", inbox_signing_key="demo-signing-key-not-a-secret",
        sandbox_root=str(tmp / "sandboxes"), repos_root=str(products_dir), evidence_dir=str(tmp / "evidence"))
    clock = clock or FakeClock()
    factory, adapters, policy, products = build_factory_and_adapters(settings, clock=clock)
    adapters["holdout"].result = holdout_result
    # Sandboxes need a real, checkoutable commit as `main`'s head, matching the
    # git mirror this harness just created -- `FakeGitHub.head_commit` would
    # otherwise fabricate an unrelated opaque sha the first time it's asked.
    adapters["github"].set_head(REPO, "main", base_commit)
    product = products.get(PRODUCT_NAME)
    return DemoContext(tmp=tmp, settings=settings, clock=clock, factory=factory, adapters=adapters,
                       policy=policy, products=products, product=product, repo=REPO, base_commit=base_commit)


__all__ = ["DemoContext", "PRODUCT_NAME", "REPO", "new_context"]
