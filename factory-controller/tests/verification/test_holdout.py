import io
import json
import zipfile

import httpx
import pytest
import respx

from factory.verification.holdout import (FakeHoldoutRunner, HoldoutRunError, WorkflowHoldoutRunner,
                                           correlation_id)

API_URL = "https://api.github.com"
REPO = "acme/holdouts"
WORKFLOW = "holdout.yml"
BLOB_URL = "https://blobstorage.example.com/artifacts/xyz?sig=abc"

PRODUCT = "acme-app"
ARTIFACT = "sha256:deadbeefcafe0123456789"
CORR_ID = correlation_id(PRODUCT, ARTIFACT)
EXPECTED_TITLE = f"holdout {CORR_ID}"


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


def mock_dispatch():
    return respx.post(f"{API_URL}/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches").mock(
        return_value=httpx.Response(204))


def mock_runs_list(run_id: int, *, title_field: str = "name"):
    return respx.get(f"{API_URL}/repos/{REPO}/actions/workflows/{WORKFLOW}/runs").mock(
        return_value=httpx.Response(200, json={
            "workflow_runs": [{"id": run_id, title_field: EXPECTED_TITLE, "status": "completed"}]}))


def mock_run_status(run_id: int, *, status: str = "completed", conclusion: str | None = "success"):
    return respx.get(f"{API_URL}/repos/{REPO}/actions/runs/{run_id}").mock(
        return_value=httpx.Response(200, json={"status": status, "conclusion": conclusion}))


def mock_artifacts_list(run_id: int, artifact_id: int):
    return respx.get(f"{API_URL}/repos/{REPO}/actions/runs/{run_id}/artifacts").mock(
        return_value=httpx.Response(200, json={"artifacts": [{"id": artifact_id, "name": "holdout-result"}]}))


def test_correlation_id_combines_product_and_a_short_artifact_hash():
    assert correlation_id("acme-app", "sha256:deadbeefcafe0123") == "acme-app-deadbeefcafe"
    # never includes the whole (potentially long) digest
    assert len(correlation_id("acme-app", "sha256:" + "a" * 64)) == len("acme-app-") + 12


@respx.mock
def test_run_success_end_to_end():
    mock_dispatch()
    mock_runs_list(123)
    mock_run_status(123)
    mock_artifacts_list(123, 55)
    respx.get(f"{API_URL}/repos/{REPO}/actions/artifacts/55/zip").mock(
        return_value=httpx.Response(200, content=make_zip({"passed": 8, "total": 10})))

    runner = make_runner()
    result = runner.run(PRODUCT, staging_url="https://staging.acme.dev", artifact=ARTIFACT)
    assert result == (8, 10)


@respx.mock
def test_dispatch_sends_correlation_id_input():
    dispatch_route = mock_dispatch()
    mock_runs_list(1)
    mock_run_status(1)
    mock_artifacts_list(1, 1)
    respx.get(f"{API_URL}/repos/{REPO}/actions/artifacts/1/zip").mock(
        return_value=httpx.Response(200, content=make_zip({"passed": 1, "total": 1})))

    runner = make_runner()
    runner.run(PRODUCT, staging_url="https://s", artifact=ARTIFACT)

    sent = json.loads(dispatch_route.calls[0].request.content)
    assert sent["inputs"]["correlation_id"] == CORR_ID


@respx.mock
def test_matches_run_by_display_title_too():
    mock_dispatch()
    mock_runs_list(7, title_field="display_title")
    mock_run_status(7)
    mock_artifacts_list(7, 1)
    respx.get(f"{API_URL}/repos/{REPO}/actions/artifacts/1/zip").mock(
        return_value=httpx.Response(200, content=make_zip({"passed": 1, "total": 1})))

    runner = make_runner()
    assert runner.run(PRODUCT, staging_url="https://s", artifact=ARTIFACT) == (1, 1)


@respx.mock
def test_ignores_runs_with_a_different_correlation_id():
    """Two dispatches (e.g. a racing concurrent run) must never be confused: only the
    run whose title matches THIS call's correlation id is picked."""
    mock_dispatch()
    respx.get(f"{API_URL}/repos/{REPO}/actions/workflows/{WORKFLOW}/runs").mock(
        return_value=httpx.Response(200, json={"workflow_runs": [
            {"id": 999, "name": "holdout some-other-app-abc123", "status": "completed"},
        ]}))
    runner = make_runner()
    with pytest.raises(HoldoutRunError, match="no run named"):
        runner.run(PRODUCT, staging_url="https://s", artifact=ARTIFACT)


@respx.mock
def test_uses_its_own_bearer_token():
    dispatch_route = mock_dispatch()
    mock_runs_list(1)
    mock_run_status(1)
    mock_artifacts_list(1, 1)
    respx.get(f"{API_URL}/repos/{REPO}/actions/artifacts/1/zip").mock(
        return_value=httpx.Response(200, content=make_zip({"passed": 1, "total": 1})))

    runner = make_runner(token="the-readonly-token")
    runner.run(PRODUCT, staging_url="https://s", artifact=ARTIFACT)
    assert dispatch_route.calls[0].request.headers["authorization"] == "Bearer the-readonly-token"


@respx.mock
def test_dispatch_failure_raises():
    respx.post(f"{API_URL}/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches").mock(
        return_value=httpx.Response(422, text="bad ref"))
    runner = make_runner()
    with pytest.raises(HoldoutRunError):
        runner.run(PRODUCT, staging_url="https://s", artifact=ARTIFACT)


@respx.mock
def test_no_matching_run_found_raises():
    mock_dispatch()
    respx.get(f"{API_URL}/repos/{REPO}/actions/workflows/{WORKFLOW}/runs").mock(
        return_value=httpx.Response(200, json={"workflow_runs": []}))
    runner = make_runner()
    with pytest.raises(HoldoutRunError):
        runner.run(PRODUCT, staging_url="https://s", artifact=ARTIFACT)


@respx.mock
def test_run_conclusion_failure_raises():
    mock_dispatch()
    mock_runs_list(9)
    mock_run_status(9, status="completed", conclusion="failure")
    runner = make_runner()
    with pytest.raises(HoldoutRunError):
        runner.run(PRODUCT, staging_url="https://s", artifact=ARTIFACT)


@respx.mock
def test_run_timeout_raises():
    mock_dispatch()
    mock_runs_list(9)
    respx.get(f"{API_URL}/repos/{REPO}/actions/runs/9").mock(
        return_value=httpx.Response(200, json={"status": "in_progress", "conclusion": None}))

    clock_values = iter([0, 0, 5000])  # deadline=0+timeout_s ; first check not expired ; second check expired
    runner = make_runner(timeout_s=1, clock=lambda: next(clock_values))
    with pytest.raises(HoldoutRunError, match="timed out"):
        runner.run(PRODUCT, staging_url="https://s", artifact=ARTIFACT)


@respx.mock
def test_no_artifact_found_raises():
    mock_dispatch()
    mock_runs_list(9)
    mock_run_status(9)
    respx.get(f"{API_URL}/repos/{REPO}/actions/runs/9/artifacts").mock(
        return_value=httpx.Response(200, json={"artifacts": []}))
    runner = make_runner()
    with pytest.raises(HoldoutRunError, match="artifact"):
        runner.run(PRODUCT, staging_url="https://s", artifact=ARTIFACT)


@respx.mock
def test_malformed_artifact_contents_raises():
    mock_dispatch()
    mock_runs_list(9)
    mock_run_status(9)
    mock_artifacts_list(9, 7)
    respx.get(f"{API_URL}/repos/{REPO}/actions/artifacts/7/zip").mock(
        return_value=httpx.Response(200, content=b"not a zip file"))
    runner = make_runner()
    with pytest.raises(HoldoutRunError):
        runner.run(PRODUCT, staging_url="https://s", artifact=ARTIFACT)


# -- Q-H2: the artifact download redirects to blob storage -------------------------------

@respx.mock
def test_artifact_download_follows_redirect_to_blob_storage():
    mock_dispatch()
    mock_runs_list(9)
    mock_run_status(9)
    mock_artifacts_list(9, 42)
    respx.get(f"{API_URL}/repos/{REPO}/actions/artifacts/42/zip").mock(
        return_value=httpx.Response(302, headers={"location": BLOB_URL}))
    respx.get(BLOB_URL).mock(
        return_value=httpx.Response(200, content=make_zip({"passed": 4, "total": 5})))

    runner = make_runner()
    result = runner.run(PRODUCT, staging_url="https://s", artifact=ARTIFACT)
    assert result == (4, 5)


@respx.mock
def test_redirect_to_blob_storage_does_not_carry_the_github_token_cross_origin():
    """httpx's own redirect handling drops Authorization when the redirect target is a
    different origin -- this pins that we rely on that (via follow_redirects=True)
    rather than accidentally forwarding the GitHub token to blob storage."""
    mock_dispatch()
    mock_runs_list(9)
    mock_run_status(9)
    mock_artifacts_list(9, 42)
    respx.get(f"{API_URL}/repos/{REPO}/actions/artifacts/42/zip").mock(
        return_value=httpx.Response(302, headers={"location": BLOB_URL}))
    blob_route = respx.get(BLOB_URL).mock(
        return_value=httpx.Response(200, content=make_zip({"passed": 1, "total": 1})))

    runner = make_runner(token="super-secret-token")
    runner.run(PRODUCT, staging_url="https://s", artifact=ARTIFACT)

    assert "authorization" not in blob_route.calls[0].request.headers


@respx.mock
def test_non_redirect_download_failure_raises():
    mock_dispatch()
    mock_runs_list(9)
    mock_run_status(9)
    mock_artifacts_list(9, 42)
    respx.get(f"{API_URL}/repos/{REPO}/actions/artifacts/42/zip").mock(return_value=httpx.Response(500))
    runner = make_runner()
    with pytest.raises(HoldoutRunError):
        runner.run(PRODUCT, staging_url="https://s", artifact=ARTIFACT)


# -- FakeHoldoutRunner ------------------------------------------------------------------

def test_fake_holdout_runner_returns_scripted_result():
    fake = FakeHoldoutRunner(result=(7, 9))
    assert fake.run(PRODUCT, staging_url="https://s", artifact=ARTIFACT) == (7, 9)
    assert fake.calls == [{"product": PRODUCT, "staging_url": "https://s", "artifact": ARTIFACT}]


def test_fake_holdout_runner_raises_scripted_error():
    fake = FakeHoldoutRunner(error=HoldoutRunError("boom"))
    with pytest.raises(HoldoutRunError):
        fake.run(PRODUCT, staging_url="https://s", artifact=ARTIFACT)
