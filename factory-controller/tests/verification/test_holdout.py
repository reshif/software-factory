import io
import json
import zipfile

import httpx
import pytest
import respx

from factory.verification.holdout import FakeHoldoutRunner, HoldoutRunError, WorkflowHoldoutRunner

API_URL = "https://api.github.com"
REPO = "acme/holdouts"
WORKFLOW = "holdout.yml"
FUTURE_CREATED_AT = "9999-01-01T00:00:00Z"  # always newer than any real dispatch timestamp


def make_zip(doc: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("holdout-result.json", json.dumps(doc))
    return buf.getvalue()


def make_runner(**kwargs) -> WorkflowHoldoutRunner:
    defaults = dict(api_url=API_URL, repo=REPO, workflow_file=WORKFLOW, token="ro-token-123",
                     poll_interval_s=0, sleep=lambda s: None)
    defaults.update(kwargs)
    return WorkflowHoldoutRunner(**defaults)


@respx.mock
def test_run_success_end_to_end():
    respx.post(f"{API_URL}/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches").mock(
        return_value=httpx.Response(204))
    respx.get(f"{API_URL}/repos/{REPO}/actions/workflows/{WORKFLOW}/runs").mock(
        return_value=httpx.Response(200, json={
            "workflow_runs": [{"id": 123, "created_at": FUTURE_CREATED_AT, "status": "in_progress"}]}))
    respx.get(f"{API_URL}/repos/{REPO}/actions/runs/123").mock(
        return_value=httpx.Response(200, json={"status": "completed", "conclusion": "success"}))
    respx.get(f"{API_URL}/repos/{REPO}/actions/runs/123/artifacts").mock(
        return_value=httpx.Response(200, json={"artifacts": [{"id": 55, "name": "holdout-result"}]}))
    respx.get(f"{API_URL}/repos/{REPO}/actions/artifacts/55/zip").mock(
        return_value=httpx.Response(200, content=make_zip({"passed": 8, "total": 10}),
                                     headers={"content-type": "application/zip"}))

    runner = make_runner()
    result = runner.run("acme-app", staging_url="https://staging.acme.dev", artifact="sha256:deadbeef")
    assert result == (8, 10)


@respx.mock
def test_uses_its_own_bearer_token():
    dispatch_route = respx.post(
        f"{API_URL}/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches").mock(
        return_value=httpx.Response(204))
    respx.get(f"{API_URL}/repos/{REPO}/actions/workflows/{WORKFLOW}/runs").mock(
        return_value=httpx.Response(200, json={
            "workflow_runs": [{"id": 1, "created_at": FUTURE_CREATED_AT, "status": "completed"}]}))
    respx.get(f"{API_URL}/repos/{REPO}/actions/runs/1").mock(
        return_value=httpx.Response(200, json={"status": "completed", "conclusion": "success"}))
    respx.get(f"{API_URL}/repos/{REPO}/actions/runs/1/artifacts").mock(
        return_value=httpx.Response(200, json={"artifacts": [{"id": 1, "name": "holdout-result"}]}))
    respx.get(f"{API_URL}/repos/{REPO}/actions/artifacts/1/zip").mock(
        return_value=httpx.Response(200, content=make_zip({"passed": 1, "total": 1})))

    runner = make_runner(token="the-readonly-token")
    runner.run("acme-app", staging_url="https://s", artifact="sha256:aa")
    assert dispatch_route.calls[0].request.headers["authorization"] == "Bearer the-readonly-token"


@respx.mock
def test_dispatch_failure_raises():
    respx.post(f"{API_URL}/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches").mock(
        return_value=httpx.Response(422, text="bad ref"))
    runner = make_runner()
    with pytest.raises(HoldoutRunError):
        runner.run("acme-app", staging_url="https://s", artifact="sha256:aa")


@respx.mock
def test_no_matching_run_found_raises():
    respx.post(f"{API_URL}/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches").mock(
        return_value=httpx.Response(204))
    respx.get(f"{API_URL}/repos/{REPO}/actions/workflows/{WORKFLOW}/runs").mock(
        return_value=httpx.Response(200, json={"workflow_runs": []}))
    runner = make_runner()
    with pytest.raises(HoldoutRunError):
        runner.run("acme-app", staging_url="https://s", artifact="sha256:aa")


@respx.mock
def test_run_conclusion_failure_raises():
    respx.post(f"{API_URL}/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches").mock(
        return_value=httpx.Response(204))
    respx.get(f"{API_URL}/repos/{REPO}/actions/workflows/{WORKFLOW}/runs").mock(
        return_value=httpx.Response(200, json={
            "workflow_runs": [{"id": 9, "created_at": FUTURE_CREATED_AT, "status": "completed"}]}))
    respx.get(f"{API_URL}/repos/{REPO}/actions/runs/9").mock(
        return_value=httpx.Response(200, json={"status": "completed", "conclusion": "failure"}))
    runner = make_runner()
    with pytest.raises(HoldoutRunError):
        runner.run("acme-app", staging_url="https://s", artifact="sha256:aa")


@respx.mock
def test_run_timeout_raises():
    respx.post(f"{API_URL}/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches").mock(
        return_value=httpx.Response(204))
    respx.get(f"{API_URL}/repos/{REPO}/actions/workflows/{WORKFLOW}/runs").mock(
        return_value=httpx.Response(200, json={
            "workflow_runs": [{"id": 9, "created_at": FUTURE_CREATED_AT, "status": "in_progress"}]}))
    respx.get(f"{API_URL}/repos/{REPO}/actions/runs/9").mock(
        return_value=httpx.Response(200, json={"status": "in_progress", "conclusion": None}))

    clock_values = iter([0, 0, 5000])  # deadline=0+timeout_s ; first check not expired ; second check expired
    runner = make_runner(timeout_s=1, clock=lambda: next(clock_values))
    with pytest.raises(HoldoutRunError, match="timed out"):
        runner.run("acme-app", staging_url="https://s", artifact="sha256:aa")


@respx.mock
def test_no_artifact_found_raises():
    respx.post(f"{API_URL}/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches").mock(
        return_value=httpx.Response(204))
    respx.get(f"{API_URL}/repos/{REPO}/actions/workflows/{WORKFLOW}/runs").mock(
        return_value=httpx.Response(200, json={
            "workflow_runs": [{"id": 9, "created_at": FUTURE_CREATED_AT, "status": "completed"}]}))
    respx.get(f"{API_URL}/repos/{REPO}/actions/runs/9").mock(
        return_value=httpx.Response(200, json={"status": "completed", "conclusion": "success"}))
    respx.get(f"{API_URL}/repos/{REPO}/actions/runs/9/artifacts").mock(
        return_value=httpx.Response(200, json={"artifacts": []}))
    runner = make_runner()
    with pytest.raises(HoldoutRunError, match="artifact"):
        runner.run("acme-app", staging_url="https://s", artifact="sha256:aa")


@respx.mock
def test_malformed_artifact_contents_raises():
    respx.post(f"{API_URL}/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches").mock(
        return_value=httpx.Response(204))
    respx.get(f"{API_URL}/repos/{REPO}/actions/workflows/{WORKFLOW}/runs").mock(
        return_value=httpx.Response(200, json={
            "workflow_runs": [{"id": 9, "created_at": FUTURE_CREATED_AT, "status": "completed"}]}))
    respx.get(f"{API_URL}/repos/{REPO}/actions/runs/9").mock(
        return_value=httpx.Response(200, json={"status": "completed", "conclusion": "success"}))
    respx.get(f"{API_URL}/repos/{REPO}/actions/runs/9/artifacts").mock(
        return_value=httpx.Response(200, json={"artifacts": [{"id": 7, "name": "holdout-result"}]}))
    respx.get(f"{API_URL}/repos/{REPO}/actions/artifacts/7/zip").mock(
        return_value=httpx.Response(200, content=b"not a zip file"))
    runner = make_runner()
    with pytest.raises(HoldoutRunError):
        runner.run("acme-app", staging_url="https://s", artifact="sha256:aa")


# -- FakeHoldoutRunner ------------------------------------------------------------------

def test_fake_holdout_runner_returns_scripted_result():
    fake = FakeHoldoutRunner(result=(7, 9))
    assert fake.run("acme-app", staging_url="https://s", artifact="sha256:aa") == (7, 9)
    assert fake.calls == [{"product": "acme-app", "staging_url": "https://s", "artifact": "sha256:aa"}]


def test_fake_holdout_runner_raises_scripted_error():
    fake = FakeHoldoutRunner(error=HoldoutRunError("boom"))
    with pytest.raises(HoldoutRunError):
        fake.run("acme-app", staging_url="https://s", artifact="sha256:aa")
