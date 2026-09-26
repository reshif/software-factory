import subprocess

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def _generate_private_key_pem() -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


@pytest.fixture(scope="session")
def rsa_private_key_path(tmp_path_factory):
    pem = _generate_private_key_pem()
    path = tmp_path_factory.mktemp("keys") / "app-private-key.pem"
    path.write_bytes(pem)
    return str(path)


def run_git(args, cwd, env=None):
    result = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, env=env)
    assert result.returncode == 0, f"git {' '.join(args)} failed: {result.stderr}"
    return result.stdout
