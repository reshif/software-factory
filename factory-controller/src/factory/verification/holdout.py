"""Black-box holdout scenarios, run from a separate repo and runner (final draft §9.2, §11).

"Black-box holdout scenarios (separate repo and runner, against the staging digest,
returns pass/fail counts only)." `WorkflowHoldoutRunner` drives that separate repo's
GitHub Actions workflow with its **own read-only identity** -- it never uses the push-
or merge-bot credentials. It:

  1. Dispatches `workflow_dispatch` on the holdout repo's workflow with
     `{"product": ..., "staging_url": ..., "artifact": ...}` inputs.
  2. Polls `GET .../actions/workflows/{workflow}/runs` for the run it just triggered
     (matched by the dispatch timestamp, since `workflow_dispatch` doesn't hand back a
     run id) until it reaches a terminal status.
  3. Downloads the `holdout-result` artifact and reads `holdout-result.json`, a JSON
     object `{"passed": int, "total": int}` -- nothing else. It never inspects the
     holdout repo's scenario bodies or logs.

A run that never completes, fails, or produces no matching artifact raises
`HoldoutRunError` -- the caller must treat that as a failed check (fail closed),
never as a passing or partial result.
"""
import io
import json
import logging
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

logger = logging.getLogger(__name__)

ARTIFACT_NAME = "holdout-result"
RESULT_FILE = "holdout-result.json"


class HoldoutRunError(Exception):
    """The holdout run could not be triggered, did not complete, or produced no result."""


@dataclass(frozen=True)
class _RunSummary:
    id: int
    status: str
    conclusion: str | None
    created_at: str


class WorkflowHoldoutRunner:
    """Drives a GitHub Actions `workflow_dispatch` holdout run over HTTPS.

    `token` must belong to the runner's own read-only identity -- separate from the
    push bot and merge bot (§11, §13.2).
    """

    def __init__(self, *, api_url: str, repo: str, workflow_file: str, token: str,
                 ref: str = "main", client: httpx.Client | None = None,
                 poll_interval_s: float = 5.0, timeout_s: float = 1800.0,
                 sleep=time.sleep, clock=time.monotonic):
        self._api_url = api_url.rstrip("/")
        self._repo = repo
        self._workflow_file = workflow_file
        self._ref = ref
        self._client = client or httpx.Client(base_url=self._api_url, headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        })
        self._poll_interval_s = poll_interval_s
        self._timeout_s = timeout_s
        self._sleep = sleep
        self._clock = clock

    def run(self, product: str, *, staging_url: str, artifact: str) -> tuple[int, int]:
        dispatched_at = self._now_iso()
        self._dispatch(product, staging_url=staging_url, artifact=artifact)
        run_id = self._find_dispatched_run(dispatched_at)
        self._wait_for_completion(run_id)
        return self._read_result(run_id)

    # -- steps -----------------------------------------------------------------------

    def _now_iso(self) -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    def _dispatch(self, product: str, *, staging_url: str, artifact: str) -> None:
        resp = self._client.post(
            f"/repos/{self._repo}/actions/workflows/{self._workflow_file}/dispatches",
            json={"ref": self._ref,
                  "inputs": {"product": product, "staging_url": staging_url, "artifact": artifact}})
        if resp.status_code not in (204, 201):
            raise HoldoutRunError(f"dispatch failed: {resp.status_code} {resp.text}")

    def _find_dispatched_run(self, dispatched_at: str, *, attempts: int = 10) -> int:
        for _ in range(attempts):
            resp = self._client.get(
                f"/repos/{self._repo}/actions/workflows/{self._workflow_file}/runs",
                params={"event": "workflow_dispatch", "per_page": 5})
            if resp.status_code != 200:
                raise HoldoutRunError(f"listing runs failed: {resp.status_code} {resp.text}")
            runs = resp.json().get("workflow_runs", [])
            candidates = [r for r in runs if r.get("created_at", "") >= dispatched_at]
            if candidates:
                newest = max(candidates, key=lambda r: r["created_at"])
                return newest["id"]
            self._sleep(self._poll_interval_s)
        raise HoldoutRunError("no workflow_dispatch run appeared after dispatching")

    def _wait_for_completion(self, run_id: int) -> None:
        deadline = self._clock() + self._timeout_s
        while True:
            resp = self._client.get(f"/repos/{self._repo}/actions/runs/{run_id}")
            if resp.status_code != 200:
                raise HoldoutRunError(f"reading run {run_id} failed: {resp.status_code} {resp.text}")
            data = resp.json()
            if data.get("status") == "completed":
                if data.get("conclusion") != "success":
                    raise HoldoutRunError(f"holdout run {run_id} concluded {data.get('conclusion')!r}")
                return
            if self._clock() >= deadline:
                raise HoldoutRunError(f"holdout run {run_id} timed out after {self._timeout_s}s")
            logger.debug("holdout run %s still %s; polling again", run_id, data.get("status"))
            self._sleep(self._poll_interval_s)

    def _read_result(self, run_id: int) -> tuple[int, int]:
        resp = self._client.get(f"/repos/{self._repo}/actions/runs/{run_id}/artifacts")
        if resp.status_code != 200:
            raise HoldoutRunError(f"listing artifacts for run {run_id} failed: {resp.status_code}")
        artifacts = resp.json().get("artifacts", [])
        match = next((a for a in artifacts if a.get("name") == ARTIFACT_NAME), None)
        if match is None:
            raise HoldoutRunError(f"run {run_id} produced no {ARTIFACT_NAME!r} artifact")

        download = self._client.get(f"/repos/{self._repo}/actions/artifacts/{match['id']}/zip")
        if download.status_code not in (200, 302):
            raise HoldoutRunError(f"downloading artifact {match['id']} failed: {download.status_code}")

        try:
            with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
                raw = archive.read(RESULT_FILE)
            doc = json.loads(raw)
            passed, total = int(doc["passed"]), int(doc["total"])
        except (zipfile.BadZipFile, KeyError, ValueError, json.JSONDecodeError) as exc:
            raise HoldoutRunError(f"malformed {RESULT_FILE} in artifact {match['id']}: {exc}") from exc
        return passed, total

    def close(self) -> None:
        self._client.close()


class FakeHoldoutRunner:
    """Scripted holdout runner for tests and `factory demo`."""

    def __init__(self, *, result: tuple[int, int] = (0, 0), error: Exception | None = None):
        self.result = result
        self.error = error
        self.calls: list[dict] = []

    def run(self, product: str, *, staging_url: str, artifact: str) -> tuple[int, int]:
        self.calls.append({"product": product, "staging_url": staging_url, "artifact": artifact})
        if self.error is not None:
            raise self.error
        return self.result
