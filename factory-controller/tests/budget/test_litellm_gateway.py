"""LiteLLMGateway against a mocked LiteLLM proxy (respx). No real network calls."""
import httpx
import pytest
import respx

from factory.budget.litellm import LiteLLMGateway

BASE_URL = "https://gateway.internal"
MASTER_KEY = "sk-master-secret"


@pytest.fixture
def gateway():
    gw = LiteLLMGateway(base_url=BASE_URL, master_key=MASTER_KEY)
    yield gw
    gw.close()


@respx.mock
def test_create_key_posts_max_budget_and_mission_metadata(gateway):
    route = respx.post(f"{BASE_URL}/key/generate").mock(
        return_value=httpx.Response(200, json={"key": "sk-mission-key-1"})
    )

    key = gateway.create_key("MIS-0042", 10.0)

    assert key == "sk-mission-key-1"
    assert route.called
    request = route.calls.last.request
    assert request.headers["authorization"] == f"Bearer {MASTER_KEY}"
    import json as _json
    body = _json.loads(request.content)
    assert body == {"max_budget": 10.0, "metadata": {"mission_id": "MIS-0042"}}


@respx.mock
def test_spent_reads_key_info(gateway):
    respx.get(f"{BASE_URL}/key/info").mock(
        return_value=httpx.Response(200, json={"key": "sk-mission-key-1", "info": {"spend": 3.75}})
    )

    spend = gateway.spent("sk-mission-key-1")

    assert spend == 3.75


@respx.mock
def test_spent_sends_key_as_query_param(gateway):
    route = respx.get(f"{BASE_URL}/key/info").mock(
        return_value=httpx.Response(200, json={"info": {"spend": 0.0}})
    )

    gateway.spent("sk-mission-key-1")

    assert route.calls.last.request.url.params["key"] == "sk-mission-key-1"


@respx.mock
def test_revoke_posts_delete(gateway):
    route = respx.post(f"{BASE_URL}/key/delete").mock(return_value=httpx.Response(200, json={"deleted_keys": 1}))

    gateway.revoke("sk-mission-key-1")

    assert route.called
    import json as _json
    body = _json.loads(route.calls.last.request.content)
    assert body == {"keys": ["sk-mission-key-1"]}


@respx.mock
def test_create_key_raises_on_http_error(gateway):
    respx.post(f"{BASE_URL}/key/generate").mock(return_value=httpx.Response(500, json={"error": "boom"}))

    with pytest.raises(httpx.HTTPStatusError):
        gateway.create_key("MIS-0042", 10.0)


@respx.mock
def test_master_key_never_appears_in_a_logged_error(gateway, caplog):
    respx.post(f"{BASE_URL}/key/generate").mock(return_value=httpx.Response(401, text="unauthorized"))

    with caplog.at_level("DEBUG"):
        with pytest.raises(httpx.HTTPStatusError):
            gateway.create_key("MIS-0042", 10.0)

    for record in caplog.records:
        assert MASTER_KEY not in record.getMessage()


@respx.mock
def test_created_key_never_appears_in_a_log_message(gateway, caplog):
    respx.post(f"{BASE_URL}/key/generate").mock(
        return_value=httpx.Response(200, json={"key": "sk-super-secret-mission-key"})
    )

    with caplog.at_level("DEBUG"):
        gateway.create_key("MIS-0042", 10.0)

    for record in caplog.records:
        assert "sk-super-secret-mission-key" not in record.getMessage()
