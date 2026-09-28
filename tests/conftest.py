"""Every test gets an isolated credential store; never read a developer's real key."""

import pytest


@pytest.fixture(autouse=True)
def isolate_factory_credentials(monkeypatch, tmp_path_factory):
    config = tmp_path_factory.mktemp("factory-auth")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("APPDATA", str(config))
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    monkeypatch.delenv("SOFTWARE_FACTORY_AUTH_DISABLED", raising=False)
