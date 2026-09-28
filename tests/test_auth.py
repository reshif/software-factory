"""Credential lifecycle, process boundaries and secret-safe failure behavior."""

import io
import json
import os
import stat
import subprocess
import sys
import warnings
from pathlib import Path
from unittest.mock import patch

import pytest

from software_factory import auth
from software_factory.checks import run_check
from software_factory.cli import _dispatch, main

KEY = "synthetic-typesafe-key-never-live"


def saved():
    auth.save_typesafe_key(KEY)
    return auth._directory() / auth.FILENAME


def test_login_is_reused_by_fresh_process_without_environment_key(tmp_path, capsys):
    with patch("sys.stdin", io.StringIO(KEY + "\n")):
        main(["auth", "login", "--stdin", "--root", str(tmp_path)])
    response = json.loads(capsys.readouterr().out)
    assert response["saved"] and response["provider_validation"] == "not_checked"
    assert KEY not in json.dumps(response)
    assert auth.KEY_ENV not in os.environ
    child = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from software_factory.auth import get_typesafe_key; "
                "assert get_typesafe_key() == 'synthetic-typesafe-key-never-live'; print('present')"
            ),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=True,
    )
    assert child.stdout.strip() == "present" and not child.stderr
    assert stat.S_IMODE(Path(response["path"]).stat().st_mode) == 0o600
    assert stat.S_IMODE(Path(response["path"]).parent.stat().st_mode) == 0o700


def test_status_missing_creates_nothing_and_logout_is_idempotent():
    directory = auth._directory()
    assert not directory.exists()
    result = auth.auth_status()
    assert result["configured"] is False and result["_exit_code"] == 2
    assert not directory.exists()
    assert auth.logout()["removed"] is False
    assert not directory.exists()


def test_rotate_and_logout_do_not_touch_unrelated_files(monkeypatch):
    path = saved()
    unrelated = path.parent / "user-settings.json"
    unrelated.write_text("user data")
    auth.save_typesafe_key("synthetic-replacement")
    assert auth.get_typesafe_key() == "synthetic-replacement"
    monkeypatch.setenv(auth.KEY_ENV, "synthetic-environment-override")
    result = auth.logout()
    assert result["removed"] and result["environment_override_present"]
    assert result["remote_key_revoked"] is False
    assert not path.exists() and unrelated.read_text() == "user data"
    assert auth.get_typesafe_key() == "synthetic-environment-override"
    assert auth.logout()["removed"] is False


def test_env_precedes_invalid_store_and_invalid_store_path(monkeypatch):
    path = saved()
    path.write_text("invalid " + KEY)
    monkeypatch.setenv(auth.KEY_ENV, "synthetic-environment")
    assert auth.get_typesafe_key() == "synthetic-environment"
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/path")
    result = auth.auth_status()
    assert result["configured"] and result["source"] == "environment" and result["path"] is None
    assert "synthetic-environment" not in json.dumps(result)


@pytest.mark.parametrize("value", ["", "   "])
def test_blank_env_is_not_an_override_and_falls_through_to_the_store(monkeypatch, value):
    # 0.3.2: a blank TYPESAFE_API_KEY (as CI templates often export it) no longer hides the saved key.
    saved()
    monkeypatch.setenv(auth.KEY_ENV, value)
    assert auth.get_typesafe_key() == KEY
    assert auth.request_credential() == (KEY, None)
    status = auth.auth_status()
    assert status["source"] == "user_store" and not status["environment_override_present"]
    monkeypatch.setenv(auth.KEY_ENV, value)
    auth.logout()
    assert auth.get_typesafe_key() is None
    assert auth.request_credential() == (None, "credential_missing")


@pytest.mark.parametrize("value", ["\n", "has space", "a\nb", "a\x00b", "é", "a" * 4097])
def test_invalid_key_does_not_create_store(value):
    directory = auth._directory()
    with pytest.raises(auth.CredentialError):
        auth.save_typesafe_key(value)
    assert not directory.exists()


@pytest.mark.parametrize("kind", ["malformed", "duplicate", "nested", "oversized", "unknown"])
def test_invalid_store_is_sanitized_and_not_overwritten(kind):
    path = saved()
    values = {
        "malformed": KEY,
        "duplicate": '{"schema_version":1,"schema_version":1,"provider":"typesafe","storage":"private-file","secret":"'
        + KEY
        + '"}',
        "nested": "[" * 2000 + '"' + KEY + '"' + "]" * 2000,
        "oversized": KEY * 2000,
        "unknown": '{"user_file":"' + KEY + '"}',
    }
    path.write_text(values[kind])
    before = path.read_bytes()
    assert auth.request_credential() == (None, "credential_unavailable")
    response = auth.auth_status()
    assert not response["configured"] and KEY not in json.dumps(response)
    with pytest.raises(auth.CredentialError) as error:
        auth.save_typesafe_key("replacement")
    assert KEY not in str(error.value) and path.read_bytes() == before


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions")
@pytest.mark.parametrize("target,mode", [("file", 0o644), ("directory", 0o755)])
def test_insecure_permissions_are_refused(target, mode):
    path = saved()
    (path if target == "file" else path.parent).chmod(mode)
    assert auth.request_credential() == (None, "credential_unavailable")
    with pytest.raises(auth.CredentialError, match="owned by you"):
        auth.save_typesafe_key("replacement")


@pytest.mark.skipif(os.name != "posix", reason="POSIX links and special files")
@pytest.mark.parametrize("kind", ["symlink", "hardlink", "fifo", "directory"])
def test_unsafe_credential_file_is_refused(tmp_path, kind):
    path = saved()
    path.unlink()
    other = tmp_path / "unrelated"
    other.write_text("preserve")
    if kind == "symlink":
        path.symlink_to(other)
    elif kind == "hardlink":
        os.link(other, path)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    else:
        path.mkdir(mode=0o700)
    assert auth.request_credential() == (None, "credential_unavailable")
    assert other.read_text() == "preserve"


def test_store_directory_symlink_is_refused(tmp_path):
    directory = auth._directory()
    directory.parent.mkdir(parents=True, exist_ok=True)
    directory.symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(auth.CredentialError, match="symlinks"):
        auth.save_typesafe_key(KEY)
    assert not (tmp_path / auth.FILENAME).exists()


@pytest.mark.parametrize("kind", ["directory", "worktree", "ancestor-symlink"])
def test_store_is_not_written_under_git_repository(tmp_path, monkeypatch, kind):
    repository = tmp_path / "repository"
    repository.mkdir()
    if kind == "worktree":
        (repository / ".git").write_text("gitdir: elsewhere")
    else:
        (repository / ".git").mkdir()
    config = repository / "config"
    config.mkdir()
    if kind == "ancestor-symlink":
        link = tmp_path / "linked-config"
        link.symlink_to(config, target_is_directory=True)
        config = link
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    with pytest.raises(auth.CredentialError, match="outside Git"):
        auth.save_typesafe_key(KEY)
    assert not (repository / "config/software-factory/credentials.json").exists()


def test_failed_atomic_rotation_keeps_previous_key(monkeypatch):
    path = saved()
    before = path.read_bytes()
    monkeypatch.setattr(auth.os, "replace", lambda *a, **kw: (_ for _ in ()).throw(OSError(KEY)))
    with pytest.raises(auth.CredentialError) as error:
        auth.save_typesafe_key("synthetic-replacement")
    assert KEY not in str(error.value)
    assert path.read_bytes() == before and auth.get_typesafe_key() == KEY
    assert list(path.parent.iterdir()) == [path]


def test_disabled_process_never_reads_or_writes_store(monkeypatch):
    saved()
    monkeypatch.setenv(auth.DISABLED_ENV, "1")
    monkeypatch.setattr(auth, "_directory", lambda: pytest.fail("disabled store access"))
    assert auth.get_typesafe_key() is None
    assert auth.request_credential() == (None, "credential_disabled")
    for command in [lambda: auth.save_typesafe_key(KEY), auth.logout, auth.login]:
        with pytest.raises(auth.CredentialError, match="disabled"):
            command()
    assert auth.auth_status()["configured"] is False


def test_product_check_cannot_auto_resolve_saved_key(tmp_path, monkeypatch):
    saved()
    monkeypatch.setenv(auth.KEY_ENV, "synthetic-env-key")
    result = run_check(
        tmp_path,
        {
            "id": "auth",
            "command": [
                sys.executable,
                "-c",
                (
                    "import os; from software_factory.auth import request_credential; "
                    "assert 'TYPESAFE_API_KEY' not in os.environ; "
                    "assert request_credential() == (None, 'credential_disabled'); print('blocked')"
                ),
            ],
        },
    )
    assert result["exit_code"] == 0 and result["stdout"].strip() == "blocked"
    assert auth.get_typesafe_key() == "synthetic-env-key"


def test_auth_global_even_when_project_has_interrupted_or_uninstalled_runtime(tmp_path, capsys):
    (tmp_path / ".factory/local").mkdir(parents=True)
    (tmp_path / ".factory/local/installation-transaction.json").write_text("{}")
    (tmp_path / ".factory/installation.json").write_text('{"uninstalled":true}')
    assert _dispatch(tmp_path, "auth", ["auth", "status"]) is None
    saved()
    main(["auth", "status", "--root", str(tmp_path)])
    result = json.loads(capsys.readouterr().out)
    assert result["configured"] and result["source"] == "user_store"
    assert KEY not in json.dumps(result)


def test_interactive_login_hides_input_and_noninteractive_requires_stdin(monkeypatch):
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", Terminal())
    monkeypatch.setattr(auth.getpass, "getpass", lambda prompt: KEY)
    assert auth.login()["saved"]
    monkeypatch.setattr(sys, "stdin", io.StringIO(KEY))
    with pytest.raises(auth.CredentialError, match="terminal"):
        auth.login()


def test_getpass_echo_fallback_is_refused(monkeypatch):
    class Terminal(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr(sys, "stdin", Terminal())

    def echo_fallback(prompt):
        warnings.warn("cannot hide input", auth.getpass.GetPassWarning)
        return KEY

    monkeypatch.setattr(auth.getpass, "getpass", echo_fallback)
    with pytest.raises(auth.CredentialError, match="no key saved"):
        auth.login()
    assert not auth._directory().exists()


def test_stdin_size_bound_and_token_argument_not_supported(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sys, "stdin", io.StringIO("a" * 4096 + " " * 100))
    with pytest.raises(auth.CredentialError, match="size limit"):
        auth.login(stdin=True)
    with pytest.raises(SystemExit):
        main(["auth", "login", "--token", KEY, "--root", str(tmp_path)])
    output = capsys.readouterr().err
    assert "Invalid auth arguments" in output and KEY not in output


def test_windows_payload_uses_protection_and_does_not_store_plaintext(monkeypatch):
    monkeypatch.setattr(auth, "WINDOWS", True)
    seen = []

    def protect(data, *, protect):
        seen.append(protect)
        assert data == (KEY.encode() if protect else b"ciphertext")
        return b"ciphertext" if protect else KEY.encode()

    monkeypatch.setattr(auth, "_dpapi", protect)
    result = auth.save_typesafe_key(KEY)
    assert result["storage"] == "windows-dpapi"
    assert KEY not in Path(result["path"]).read_text()
    assert auth.get_typesafe_key() == KEY
    assert seen == [True, False]


def test_global_auth_does_not_discover_project(monkeypatch, capsys):
    saved()
    monkeypatch.setattr(
        "software_factory.cli.resolve_root", lambda *_: pytest.fail("auth discovered project")
    )
    main(["auth", "status"])
    assert json.loads(capsys.readouterr().out)["configured"]


def test_unresolvable_storage_is_a_sanitized_failure(tmp_path, monkeypatch):
    link = tmp_path / "loop"
    link.symlink_to(link)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(link))
    assert auth.request_credential() == (None, "credential_unavailable")


def test_rotation_after_open_keeps_old_descriptor_readable(monkeypatch):
    path = saved()
    original_open = auth.os.open
    rotate = True

    def open_and_rotate(name, flags, *args, **kwargs):
        nonlocal rotate
        descriptor = original_open(name, flags, *args, **kwargs)
        if name == auth.FILENAME and rotate:
            rotate = False
            replacement = path.with_name("rotated.json")
            value = json.loads(path.read_text())
            value["secret"] = "synthetic-rotated-key"
            replacement.write_text(json.dumps(value))
            replacement.chmod(0o600)
            os.replace(replacement, path)
            assert os.fstat(descriptor).st_nlink == 0
        return descriptor

    monkeypatch.setattr(auth.os, "open", open_and_rotate)
    assert auth.get_typesafe_key() == KEY
    assert auth.get_typesafe_key() == "synthetic-rotated-key"


def test_rotation_during_path_stat_accepts_unlinked_inode(monkeypatch):
    from types import SimpleNamespace

    saved()
    original_stat = auth.os.stat

    def stat_during_rotation(name, *args, **kwargs):
        info = original_stat(name, *args, **kwargs)
        if name == auth.FILENAME:
            # Concurrent replacement may unlink the inode while stat holds it.
            return SimpleNamespace(
                st_mode=info.st_mode,
                st_uid=info.st_uid,
                st_size=info.st_size,
                st_nlink=0,
            )
        return info

    monkeypatch.setattr(auth.os, "stat", stat_during_rotation)
    assert auth.get_typesafe_key() == KEY


@pytest.mark.parametrize("kind", ["malformed", "oversized", "unknown"])
def test_logout_removes_an_invalid_store_after_safety_checks(kind):
    path = saved()
    unrelated = path.parent / "user-settings.json"
    unrelated.write_text("user data")
    path.write_text({"malformed": KEY, "oversized": KEY * 2000, "unknown": '{"a": 1}'}[kind])
    with pytest.raises(auth.CredentialError):
        auth.get_typesafe_key()
    assert auth.logout()["removed"] is True
    assert not path.exists() and unrelated.read_text() == "user data"
    assert auth.get_typesafe_key() is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX permissions and symlinks")
def test_logout_still_refuses_unsafe_invalid_store(tmp_path):
    path = saved()
    path.write_text("invalid")
    path.chmod(0o644)
    with pytest.raises(auth.CredentialError, match="owned by you"):
        auth.logout()
    assert path.exists()
    path.unlink()
    target = tmp_path / "elsewhere.json"
    target.write_text("invalid")
    path.symlink_to(target)
    with pytest.raises(auth.CredentialError, match="symlinks"):
        auth.logout()
    assert path.is_symlink() and target.read_text() == "invalid"


def test_request_credential_reports_os_errors_as_unavailable():
    def broken():
        raise PermissionError("denied")

    assert auth.request_credential(broken) == (None, "credential_unavailable")


def test_oversized_key_reports_size_before_format(monkeypatch):
    with pytest.raises(auth.CredentialError, match="size limit"):
        auth.save_typesafe_key("a" * 4097)
    with pytest.raises(auth.CredentialError, match="size limit"):
        auth.save_typesafe_key("a b" * 2000)
    monkeypatch.setattr(sys, "stdin", io.StringIO("a" * 4097))
    with pytest.raises(auth.CredentialError, match="size limit"):
        auth.login(stdin=True)


def test_store_body_errors_are_not_relabelled_as_permission_problems(monkeypatch):
    saved()

    class Marker(OSError):
        pass

    with pytest.raises(Marker), auth._store():
        raise Marker("body failure")
    real_open = os.open

    def failing_open(path, flags, *args, **kwargs):
        if path == auth.FILENAME or str(path).endswith(auth.FILENAME):
            raise OSError("disk error")
        return real_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", failing_open)
    with pytest.raises(auth.CredentialError, match="Cannot read the credential file"):
        auth.get_typesafe_key()
    assert auth.request_credential() == (None, "credential_unavailable")
