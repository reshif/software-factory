"""The real production path for a webhook, with no stand-in Factory:

signed HTTP POST -> app.py -> Factory.enqueue_webhook -> durable store queue
-> (a SEPARATE Factory instance, like the worker process) dispatch_webhooks -> mission.

Guards the contract between app.py (which names the queued event) and
Factory.dispatch_webhooks (which rebuilds it), and the serve/worker split.
"""
import hashlib
import hmac
import json
import shutil

from fastapi.testclient import TestClient

from factory.app import create_app
from factory.config import Settings
from factory.models import WorkItem
from factory.policy.loader import default_kit_dir
from factory.wiring import build_factory, build_factory_and_adapters

SECRET = "e2e-webhook-secret"
REPO = "org/backend-service"


def _settings(tmp_path):
    products = tmp_path / "products"
    shutil.copytree(default_kit_dir() / "templates" / "backend-service", products / "backend-service")
    return Settings(mode="local", github_webhook_secret=SECRET, products_dir=str(products),
                    repos_root=str(tmp_path / "repos"), sandbox_root=str(tmp_path / "sbx"),
                    evidence_dir=str(tmp_path / "evidence"))


def _post(client, payload, delivery):
    body = json.dumps(payload).encode()
    return client.post("/webhooks/github", content=body, headers={
        "X-GitHub-Event": "issues", "X-GitHub-Delivery": delivery, "Content-Type": "application/json",
        "X-Hub-Signature-256": "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()})


def test_signed_webhook_reaches_a_separate_worker_factory_and_starts_one_mission(tmp_path):
    settings = _settings(tmp_path)
    serve_factory, adapters, _, _ = build_factory_and_adapters(settings)
    adapters["github"].add_issue(WorkItem(item_id=f"{REPO}#42", product="backend-service", repo=REPO,
                                          number=42, title="Add a thing", body="please",
                                          labels=("factory:feature",), author="dev"))
    worker_factory = build_factory(settings, adapters=adapters)   # same store, different instance
    assert worker_factory is not serve_factory

    client = TestClient(create_app(settings, factory=serve_factory))
    payload = {"action": "labeled", "repository": {"full_name": REPO},
               "issue": {"number": 42, "title": "Add a thing", "body": "please", "user": {"login": "dev"}},
               "label": {"name": "factory:feature"}, "sender": {"login": "dev", "type": "User"}}
    assert _post(client, payload, "delivery-1").status_code == 204
    assert _post(client, payload, "delivery-1").status_code == 204          # GitHub redelivery: deduped

    worker_factory.dispatch_webhooks()

    store = adapters["store"]
    missions = store.list_missions()
    assert len(missions) == 1, [m.state for m in missions]
    assert missions[0].work_item_id == f"{REPO}#42"
    assert missions[0].state != "NEW"
    assert store.claim_webhooks() == []                                     # acked, not poisoned
    assert not any(e["kind"] == "webhook_poisoned" for e in store.list_events("_webhooks"))
