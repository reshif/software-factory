import httpx
import jwt
import respx

from factory.github.app_auth import AppCredentials, InstallationTokenProvider


def make_creds(rsa_private_key_path, app_id="123", installation_id="456"):
    return AppCredentials(app_id=app_id, private_key_path=rsa_private_key_path,
                          installation_id=installation_id)


@respx.mock
def test_mints_a_token_and_signs_a_valid_jwt(rsa_private_key_path):
    creds = make_creds(rsa_private_key_path)
    captured = {}

    def responder(request):
        captured["auth_header"] = request.headers["Authorization"]
        return httpx.Response(201, json={"token": "tok-1", "expires_at": "2099-01-01T00:10:00Z"})

    respx.post("https://api.github.com/app/installations/456/access_tokens").mock(side_effect=responder)

    provider = InstallationTokenProvider(creds, client=httpx.Client())
    token = provider.token()

    assert token == "tok-1"
    scheme, jwt_text = captured["auth_header"].split(" ", 1)
    assert scheme == "Bearer"
    public_key = _public_key_for(rsa_private_key_path)
    claims = jwt.decode(jwt_text, public_key, algorithms=["RS256"])
    assert claims["iss"] == "123"
    assert 0 < claims["exp"] - claims["iat"] <= 600


@respx.mock
def test_caches_token_until_near_expiry(rsa_private_key_path, monkeypatch):
    creds = make_creds(rsa_private_key_path)
    route = respx.post("https://api.github.com/app/installations/456/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "tok-1", "expires_at": "2099-01-01T00:10:00Z"})
    )
    # Deterministic ttl regardless of wall-clock date.
    monkeypatch.setattr("factory.github.app_auth._parse_expiry_seconds", lambda expires_at: 120.0)

    clock = {"t": 1_000.0}
    provider = InstallationTokenProvider(creds, client=httpx.Client(), monotonic=lambda: clock["t"])

    assert provider.token() == "tok-1"
    assert route.call_count == 1

    clock["t"] += 30  # well within the 60s safety margin before (120 - 60) = 60s elapse
    assert provider.token() == "tok-1"
    assert route.call_count == 1


@respx.mock
def test_refreshes_after_safety_margin_elapses(rsa_private_key_path, monkeypatch):
    creds = make_creds(rsa_private_key_path)
    route = respx.post("https://api.github.com/app/installations/456/access_tokens").mock(
        side_effect=[
            httpx.Response(201, json={"token": "tok-1", "expires_at": "2099-01-01T00:10:00Z"}),
            httpx.Response(201, json={"token": "tok-2", "expires_at": "2099-01-01T00:10:00Z"}),
        ]
    )
    monkeypatch.setattr("factory.github.app_auth._parse_expiry_seconds", lambda expires_at: 120.0)

    clock = {"t": 1_000.0}
    provider = InstallationTokenProvider(creds, client=httpx.Client(), monotonic=lambda: clock["t"])

    assert provider.token() == "tok-1"
    clock["t"] += 61  # past (120 - 60) = 60s: must refresh
    assert provider.token() == "tok-2"
    assert route.call_count == 2


@respx.mock
def test_token_text_never_logged(rsa_private_key_path, caplog):
    creds = make_creds(rsa_private_key_path)
    respx.post("https://api.github.com/app/installations/456/access_tokens").mock(
        return_value=httpx.Response(201, json={"token": "super-secret-token", "expires_at": "2099-01-01T00:10:00Z"})
    )
    with caplog.at_level("DEBUG"):
        InstallationTokenProvider(creds, client=httpx.Client()).token()

    for record in caplog.records:
        assert "super-secret-token" not in record.getMessage()


def test_context_manager_closes_owned_client_only(rsa_private_key_path):
    creds = make_creds(rsa_private_key_path)
    owned = InstallationTokenProvider(creds)
    with owned as provider:
        assert provider is owned
    assert owned._client.is_closed is True

    injected_client = httpx.Client()
    injected = InstallationTokenProvider(creds, client=injected_client)
    injected.close()
    assert injected_client.is_closed is False
    injected_client.close()


def _public_key_for(private_key_path):
    from cryptography.hazmat.primitives import serialization

    with open(private_key_path, "rb") as f:
        private_key = serialization.load_pem_private_key(f.read(), password=None)
    return private_key.public_key()
