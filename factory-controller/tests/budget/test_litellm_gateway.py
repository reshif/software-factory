"""LiteLLMGateway against a mocked LiteLLM proxy (respx). No real network calls."""
import json as _json

import httpx
import pytest
import respx

from factory.budget.litellm import LiteLLMGateway, LiteLLMGatewayError

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
def test_spent_uses_documented_get_and_never_logs_the_key(gateway, caplog):
    """LiteLLM documents GET /key/info?key=...; R-A4: the key must not reach our logs."""
    import logging
    caplog.set_level(logging.DEBUG)
    route = respx.get(f"{BASE_URL}/key/info").mock(
        return_value=httpx.Response(200, json={"info": {"spend": 0.0}})
    )

    gateway.spent("sk-mission-key-1")

    assert route.calls.last.request.url.params["key"] == "sk-mission-key-1"
    assert "sk-mission-key-1" not in caplog.text


@respx.mock
def test_transport_error_does_not_leak_the_url(gateway):
    respx.get(f"{BASE_URL}/key/info").mock(side_effect=httpx.ConnectError("boom"))

    with pytest.raises(LiteLLMGatewayError) as excinfo:
        gateway.spent("sk-mission-key-should-not-leak")

    assert "sk-mission-key-should-not-leak" not in str(excinfo.value)
    assert "gateway.internal" not in str(excinfo.value)


@respx.mock
def test_revoke_posts_delete(gateway):
    route = respx.post(f"{BASE_URL}/key/delete").mock(return_value=httpx.Response(200, json={"deleted_keys": 1}))

    gateway.revoke("sk-mission-key-1")

    assert route.called
    body = _json.loads(route.calls.last.request.content)
    assert body == {"keys": ["sk-mission-key-1"]}


@respx.mock
def test_create_key_raises_gateway_error_on_http_error(gateway):
    respx.post(f"{BASE_URL}/key/generate").mock(return_value=httpx.Response(500, json={"error": "boom"}))

    with pytest.raises(LiteLLMGatewayError):
        gateway.create_key("MIS-0042", 10.0)


@respx.mock
def test_error_message_has_no_scheme_host_or_query(gateway):
    respx.post(f"{BASE_URL}/key/generate").mock(return_value=httpx.Response(500, json={"error": "boom"}))

    with pytest.raises(LiteLLMGatewayError) as excinfo:
        gateway.create_key("MIS-0042", 10.0)

    message = str(excinfo.value)
    assert "gateway.internal" not in message
    assert "https://" not in message
    assert "?" not in message


@respx.mock
def test_spent_error_message_redacts_the_key(gateway):
    respx.get(f"{BASE_URL}/key/info").mock(return_value=httpx.Response(404, json={"error": "not found"}))

    with pytest.raises(LiteLLMGatewayError) as excinfo:
        gateway.spent("sk-mission-key-should-not-leak")

    assert "sk-mission-key-should-not-leak" not in str(excinfo.value)


@respx.mock
def test_master_key_never_appears_in_a_logged_error(gateway, caplog):
    respx.post(f"{BASE_URL}/key/generate").mock(return_value=httpx.Response(401, text="unauthorized"))

    with caplog.at_level("DEBUG"):
        with pytest.raises(LiteLLMGatewayError):
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
