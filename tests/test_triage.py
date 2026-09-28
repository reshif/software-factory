"""Advisory check triage. All provider calls are mocked, with no paid inference."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from software_factory import cli, triage
from software_factory.checks import verify_mission
from software_factory.core import FactoryError, asset_root, hash_file, write_json
from software_factory.jev import JevError
from software_factory.redaction import redact
from software_factory.workflow import (
    accept_scope,
    add_task,
    create_mission,
    record_decision,
    transition_mission,
    transition_task,
)

SECRETS = ("sk-TESTSECRET123456", "abcd1234efgh5678")
FAILING = (
    "import sys; print('Authorization: Bearer sk-TESTSECRET123456'); "
    "print('api_key=abcd1234efgh5678', file=sys.stderr); "
    "print('AssertionError: expected 2, got 3', file=sys.stderr); sys.exit(1)"
)


def git(root, *args):
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, check=False, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def config_for(jev=None, extra_checks=()):
    return {
        "schema_version": 1,
        "name": "triage-fixture",
        "profile": "codex",
        "completion_target": "READY_PR",
        "work_types": ["feature"],
        "limits": {"repair_attempts": 1, "parallel_writers": 1, "check_timeout_seconds": 10},
        "checks": [
            {
                "id": "unit",
                "command": [sys.executable, "-c", FAILING, "--token=sk-TESTSECRET123456"],
                "cwd": ".",
                "required": True,
                "timeout_seconds": 10,
            },
            {
                "id": "lint",
                "command": [sys.executable, "-c", "print('lint clean')"],
                "cwd": ".",
                "required": True,
                "timeout_seconds": 10,
            },
            *extra_checks,
        ],
        "owners": {"maintainer": "implementer", "reviewer": "reviewer"},
        "delivery": {
            "enabled": False,
            "staging_command": None,
            "release_command": None,
            "recovery_command": None,
        },
        "evidence_exclude": [".factory/missions/", ".factory/local/"],
        "jev": {
            "enabled": True,
            "provider": "typesafe",
            "model": "jev-1.13.0",
            "claim_mode": "advisory",
            **(jev or {}),
        },
    }


def snapshot(root):
    """Bytes of every mission record and evidence file, including private run logs."""
    result = {}
    for base in (".factory/missions", ".factory/local/runs"):
        for path in sorted((root / base).rglob("*")):
            if path.is_file():
                result[path.relative_to(root).as_posix()] = path.read_bytes()
    return result


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    (root / ".factory").mkdir(parents=True)
    for name in ("CONSTITUTION.md", "workflow.json", "policy.json"):
        shutil.copyfile(asset_root() / name, root / ".factory" / name)
    (root / ".gitignore").write_text(".factory/local/\n__pycache__/\n")
    (root / "src").mkdir()
    (root / "src/app.py").write_text("VALUE = 1\n")
    write_json(root, "factory.json", config_for())
    git(root, "init", "-q", "-b", "main")
    git(root, "add", ".")
    git(root, "-c", "user.name=T", "-c", "user.email=t@example.invalid", "commit", "-qm", "baseline")
    create_mission(root, {"id": "M-ONE", "title": "Fixture change", "kind": "feature"})
    path = root / ".factory/missions/M-ONE"
    (path / "spec.md").write_text("# Specification\n\nChange VALUE.\n\n## Risks\n\nNone.\n")
    record_decision(
        root,
        "M-ONE",
        {
            "id": "D-SCOPE",
            "kind": "scope",
            "reference": "User authorized fixture specification",
            "subject_hash": hash_file(root, ".factory/missions/M-ONE/spec.md"),
        },
    )
    accept_scope(root, "M-ONE")
    add_task(root, "M-ONE", {"id": "T-ONE", "title": "Change", "owned_paths": ["src/**"], "checks": ["unit"]})
    transition_mission(root, "M-ONE", "IMPLEMENTING")
    transition_task(root, "M-ONE", "T-ONE", "RUNNING")
    verified = verify_mission(root, "M-ONE", "R-ONE")
    assert not verified["pass"]
    return root


def set_jev(root, **values):
    config = json.loads((root / "factory.json").read_text())
    config["jev"].update(values)
    write_json(root, "factory.json", config)


class Provider:
    def __init__(self, choice="flaky", confidence=0.8, error=None):
        self.choice, self.confidence, self.error = choice, confidence, error
        self.bodies = []

    def __call__(self, body, **options):
        assert options["deadline_ms"] == 3000 and options["max_response_bytes"] == 65536
        self.bodies.append(json.dumps(body, ensure_ascii=False))
        if self.error:
            raise self.error
        options["on_attempt"](1)
        return {
            "model": body["model"],
            "answers": {
                key: {
                    "type": "choice",
                    "choice": self.choice,
                    "confidence": self.confidence,
                    "probabilities": {
                        option: 0.75 if option == self.choice else 0.05 for option in question["criteria"]
                    },
                }
                for key, question in body["questions"].items()
            },
            "usage": {"input_tokens": 50, "output_tokens": 0},
        }


def run(root, provider=None, **options):
    return triage.triage_checks(
        root,
        "M-ONE",
        "R-ONE",
        get_api_key=lambda: "test-key-not-real",
        request=provider or Provider(),
        **options,
    )


def test_only_failing_checks_are_sent_with_secrets_masked(project):
    provider = Provider()
    result = run(project, provider)
    assert len(provider.bodies) == 1
    body = json.loads(provider.bodies[0])
    assert [item["check"] for item in body["state"]["checks"]] == ["unit"]
    assert list(body["questions"]) == ["q0"]
    assert set(body["questions"]["q0"]["criteria"]) == {
        "flaky",
        "environment",
        "test_defect",
        "product_defect",
        "configuration",
        "abstain",
    }
    sent = body["state"]["checks"][0]
    assert sent["status"] == "fail" and sent["exit_code"] == 1
    assert isinstance(sent["argv"], list) and isinstance(sent["duration_ms"], int)
    assert "expected 2, got 3" in sent["log_tail"]["stderr"]
    for secret in SECRETS:
        assert secret not in provider.bodies[0]
    assert "test-key-not-real" not in provider.bodies[0]
    assert result["status"] == "complete" and result["coverage"]["failing"] == 1


def test_confident_choice_is_classified(project):
    result = run(project, Provider("flaky", 0.8))
    assert result["checks"] == [
        {"check": "unit", "category": "flaky", "confidence": 0.8, "review": "classified"}
    ]
    assert "_exit_code" not in result


def test_low_confidence_and_abstain_are_unclassified(project):
    low = run(project, Provider("flaky", 0.4))["checks"][0]
    assert low["category"] is None and low["review"] == "unclassified" and low["confidence"] == 0.4
    abstained = run(project, Provider("abstain", 0.95))["checks"][0]
    assert abstained["category"] is None and abstained["review"] == "unclassified"
    assert abstained["reason"] == "abstained"


def test_configured_threshold_is_used(project):
    set_jev(project, triage_accept_confidence=0.3)
    assert run(project, Provider("environment", 0.4))["checks"][0]["review"] == "classified"


def test_invalid_threshold_is_an_error(project):
    set_jev(project, triage_accept_confidence=1.5)
    provider = Provider()
    with pytest.raises(FactoryError, match="Invalid factory"):
        run(project, provider)
    base = config_for()
    for value in (True, -0.1, float("nan"), "0.7"):
        base["jev"]["triage_accept_confidence"] = value
        with pytest.raises(FactoryError, match="triage_accept_confidence"):
            triage.triage_settings(base)
    assert not provider.bodies


def test_disabled_and_no_network_make_no_request(project):
    provider = Provider()
    assert run(project, provider, no_network=True) == {
        "schema_version": 1,
        "operation": "check_triage",
        "status": "unavailable",
        "reason": "network_disallowed",
        "advisory_only": True,
        "_exit_code": 2,
    }
    set_jev(project, enabled=False)
    result = run(project, provider)
    assert result["reason"] == "disabled" and result["_exit_code"] == 2
    assert not provider.bodies
    assert not (project / triage.LOCAL).exists()


def test_log_hash_mismatch_is_refused(project):
    log = project / ".factory/local/runs/M-ONE/R-ONE/unit.stderr.log"
    log.write_bytes(log.read_bytes() + b"tampered\n")
    provider = Provider()
    with pytest.raises(FactoryError, match="hash does not match") as caught:
        run(project, provider)
    assert caught.value.exit_code == 2 and not provider.bodies


def test_unregistered_evidence_and_unknown_check_are_refused(project):
    with pytest.raises(FactoryError, match="not registered"):
        triage.triage_checks(project, "M-ONE", "R-MISSING", request=Provider())
    with pytest.raises(FactoryError, match="not in evidence"):
        run(project, check="nope")


def test_selected_passing_check_sends_nothing(project):
    provider = Provider()
    result = run(project, provider, check="lint")
    assert result["status"] == "nothing_to_triage" and not provider.bodies


def test_shadow_leaks_nothing_and_report_stores_only_hashes(project):
    set_jev(project, claim_mode="shadow")
    result = run(project, Provider("product_defect", 0.9))
    text = json.dumps(result)
    assert result["judgments_withheld"] is True and "checks" not in result
    for leaked in ("product_defect", "0.9", "classified", "unit"):
        assert leaked not in text
    report_bytes = (project / result["report"]).read_bytes()
    assert hashlib.sha256(report_bytes).hexdigest() == result["report_hash"]
    report = json.loads(report_bytes)
    assert report["checks"][0]["category"] == "product_defect"
    for secret in (*SECRETS, "test-key-not-real", "expected 2, got 3", "Bearer"):
        assert secret.encode() not in report_bytes
    assert report["checks"][0]["masked"] >= 2
    assert (project / result["report"]).stat().st_mode & 0o077 == 0


def test_mission_state_and_evidence_bytes_are_unchanged(project):
    before = snapshot(project)
    run(project, Provider("flaky", 0.9))
    run(project, Provider("flaky", 0.2))
    run(project, Provider(error=JevError("http_503")))
    assert snapshot(project) == before


def test_provider_errors_and_limits_are_reported(project):
    result = run(project, Provider(error=JevError("deadline_exceeded")))
    assert result["status"] == "unavailable" and result["_exit_code"] == 2
    assert result["checks"][0]["category"] is None
    result = run(project, Provider(error=RuntimeError("secret sk-TESTSECRET123456")))
    assert result["reason"] == "network_error" and "sk-TEST" not in json.dumps(result)
    result = triage.triage_checks(project, "M-ONE", "R-ONE", get_api_key=lambda: None, request=Provider())
    assert result["reason"] == "credential_missing"
    set_jev(project, max_request_bytes=1024)
    provider = Provider()
    result = run(project, provider)
    assert result["status"] == "unresolved" and result["reason"] == "request_too_large"
    assert not provider.bodies


def test_invalid_provider_answer_is_rejected(project):
    class Bad(Provider):
        def __call__(self, body, **options):
            value = super().__call__(body, **options)
            value["answers"]["q0"]["choice"] = "supports"
            return value

    result = run(project, Bad())
    assert result["status"] == "invalid" and result["checks"][0]["category"] is None


def test_cli_registers_triage_and_keeps_checks(project, capsys):
    parser = cli.build_parser()
    checks = parser.parse_args(["checks", "--only", "unit", "--require-clean"])
    assert (checks.command, checks.only, checks.require_clean) == ("checks", "unit", True)
    args = parser.parse_args(["triage", "--mission", "M-ONE", "--revision", "R-ONE", "--no-network"])
    assert args.handler is triage.handler and args.check is None
    with patch("software_factory.cli._dispatch", return_value=None):
        with pytest.raises(SystemExit) as caught:
            cli.main(
                [
                    "--root",
                    str(project),
                    "triage",
                    "--mission",
                    "M-ONE",
                    "--revision",
                    "R-ONE",
                    "--no-network",
                ]
            )
        assert caught.value.code == 2
        assert json.loads(capsys.readouterr().out)["reason"] == "network_disallowed"
        cli.main(["--root", str(project), "checks", "--only", "lint"])
        output = json.loads(capsys.readouterr().out)
    assert output["scope"] == "focused" and [c["id"] for c in output["checks"]] == ["lint"]
    assert output["pass"] is True


def test_packaged_schema_declares_threshold():
    schema = json.loads((Path(triage.__file__).parent / "data/schemas/factory.schema.json").read_text())
    prop = schema["properties"]["jev"]["properties"]["triage_accept_confidence"]
    assert (prop["minimum"], prop["maximum"], prop["default"]) == (0, 1, 0.6)


def test_log_tail_is_bounded_and_starts_on_a_line():
    data = b"x" * 5000 + b"\nsecret-half-line api_key=abcd1234efgh5678\n" + b"y\n" * 4000
    tail = triage._trim(data[-triage.TAIL_BYTES :], len(data))
    assert len(tail) < triage.TAIL_BYTES and data.endswith(tail) and tail.startswith(b"y\n")
    assert triage._trim(b"short\n", 6) == b"short\n"
    text = triage._log_tail({"stdout": "", "stderr": tail.decode()}, 3072)["stderr"]
    assert len(text.encode()) <= 3072 and text.startswith("y\n")


LEAKS = {
    "dsn": "Dsn-pass-9431",
    "stripe": "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc",
    "jwt": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36P",
    "google": "AIza" + "SyD-9tSrke72PouQMnMX-a7eZSW0jkFMBWY",
    "phrase": "horse battery staple",
    "flag": "Flag-pass-5521",
}
LEAKY = (
    "import sys; "
    f"print('connect postgres://app:{LEAKS['dsn']}@db:5432/app'); "
    f"print('STRIPE={LEAKS['stripe']} session={LEAKS['jwt']}', file=sys.stderr); "
    f"print('maps {LEAKS['google']}', file=sys.stderr); "
    f"print('password = \"correct {LEAKS['phrase']}\"', file=sys.stderr); sys.exit(1)"
)


def test_additional_secret_shapes_in_logs_and_argv_are_masked(project):
    leaky = {
        "id": "leaky",
        "command": [
            sys.executable,
            "-c",
            LEAKY,
            "--password",
            LEAKS["flag"],
            f"--dsn=redis://:{LEAKS['dsn']}@h",
        ],
        "cwd": ".",
        "required": True,
        "timeout_seconds": 10,
    }
    write_json(project, "factory.json", config_for(extra_checks=[leaky]))
    assert not verify_mission(project, "M-ONE", "R-TWO")["pass"]
    provider = Provider()
    result = triage.triage_checks(
        project,
        "M-ONE",
        "R-TWO",
        check="leaky",
        get_api_key=lambda: "test-key-not-real",
        request=provider,
    )
    assert result["status"] == "complete"
    sent = provider.bodies[0]
    for secret in (*LEAKS.values(), "correct horse", "staple"):
        assert secret not in sent
    body = json.loads(sent)["state"]["checks"][0]
    assert "[REDACTED:url_password]" in body["log_tail"]["stdout"]
    assert body["argv"][3:5] == ["--password", "[REDACTED:password_flag]"]


def test_secret_straddling_the_final_tail_cut_is_masked_first(monkeypatch):
    trailer = "".join(f"trailer line {n:04d}\n" for n in range(400))
    trailer = trailer[len(trailer) - (triage.TAIL_BYTES - 5) :]
    trailer = trailer[trailer.index("\n") + 1 :]
    secret = f'password = "correct {LEAKS["phrase"]}" {LEAKS["stripe"]}\n'
    masked_line = redact(secret)[0]
    # The final cut lands inside the masked secret line, even inside its last mask.
    assert 0 < len(masked_line) + len(trailer) - triage.TAIL_BYTES < len(masked_line)
    logs = {"stdout": b"", "stderr": ("early line\n" * 3000 + secret + trailer).encode()}

    def read_log(root, log, path):
        data = logs[path.rsplit(".", 2)[-2]]
        return data[-triage.READ_BYTES :], len(data)

    monkeypatch.setattr(triage, "_read_log", read_log)
    item = {"id": "unit", "stdout_log": {"sha256": "0" * 64}, "stderr_log": {"sha256": "1" * 64}}
    masked, masks, _ = triage._excerpts(Path("."), "M-ONE", "R-ONE", item)
    assert masks == 2
    assert triage._log_tail(masked, triage.TAIL_BYTES) == {"stdout": "", "stderr": trailer}


NEW_LEAKS = {
    "azure": "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MA==",
    "db_pass": "Db-pass-7710",
    "hf": "hf_" + "a1B2c3D4" * 4,
    "curl": "Curl-pass-3318",
    "argv_curl": "Argv-curl-6604",
}
NEW_LEAKY = (
    "import sys; "
    f"print('AccountName=a;AccountKey={NEW_LEAKS['azure']};EndpointSuffix=x'); "
    f"print('DB_PASS={NEW_LEAKS['db_pass']} model {NEW_LEAKS['hf']}', file=sys.stderr); "
    f"print('curl -u alice:{NEW_LEAKS['curl']} https://h', file=sys.stderr); sys.exit(1)"
)


def test_further_secret_shapes_in_logs_and_curl_argv_are_masked(project):
    leaky = {
        "id": "leaky",
        "command": [sys.executable, "-c", NEW_LEAKY, "curl", "-u", f"alice:{NEW_LEAKS['argv_curl']}"],
        "cwd": ".",
        "required": True,
        "timeout_seconds": 10,
    }
    write_json(project, "factory.json", config_for(extra_checks=[leaky]))
    assert not verify_mission(project, "M-ONE", "R-TWO")["pass"]
    provider = Provider()
    result = triage.triage_checks(
        project,
        "M-ONE",
        "R-TWO",
        check="leaky",
        get_api_key=lambda: "test-key-not-real",
        request=provider,
    )
    assert result["status"] == "complete"
    sent = provider.bodies[0]
    for secret in NEW_LEAKS.values():
        assert secret not in sent
    body = json.loads(sent)["state"]["checks"][0]
    assert "AccountKey=[REDACTED:connection_string]" in body["log_tail"]["stdout"]
    assert "DB_PASS=[REDACTED:assignment]" in body["log_tail"]["stderr"]
    assert "[REDACTED:huggingface_token]" in body["log_tail"]["stderr"]
    assert "curl -u alice:[REDACTED:password_flag]" in body["log_tail"]["stderr"]
    assert body["argv"][3:6] == ["curl", "-u", "alice:[REDACTED:password_flag]"]


def test_triage_uses_saved_credential_and_reports_store_failure(project):
    from software_factory import auth

    key = "synthetic-triage-stored-key"
    auth.save_typesafe_key(key)
    provider = Provider()

    def request(body, **options):
        assert options["api_key"] == key
        return provider(body, **options)

    result = triage.triage_checks(project, "M-ONE", "R-ONE", request=request)
    assert provider.bodies and result["status"] == "complete"
    assert key not in json.dumps(result)
    (auth._directory() / auth.FILENAME).write_text("broken-store")
    result = triage.triage_checks(project, "M-ONE", "R-ONE", request=provider)
    assert result["reason"] == "credential_unavailable" and result["_exit_code"] == 2


# --- 0.3.2 hardening ---


def failing_checks(count):
    script = "import sys; print('line ' * 2000, file=sys.stderr); sys.exit(1)"
    return [
        {
            "id": f"extra{index}",
            "command": [sys.executable, "-c", script],
            "cwd": ".",
            "required": True,
            "timeout_seconds": 10,
        }
        for index in range(count)
    ]


def test_eight_failing_checks_fit_one_request_with_scaled_tails(project):
    write_json(project, "factory.json", config_for(extra_checks=failing_checks(8)))
    assert not verify_mission(project, "M-ONE", "R-TWO")["pass"]
    provider = Provider("flaky", 0.9)
    result = triage.triage_checks(
        project, "M-ONE", "R-TWO", get_api_key=lambda: "test-key-not-real", request=provider
    )
    assert len(provider.bodies) == 1
    assert len(provider.bodies[0].encode()) <= 24576
    assert len(json.loads(provider.bodies[0])["state"]["checks"]) == triage.MAX_CHECKS
    assert result["coverage"]["evaluated"] == triage.MAX_CHECKS and result["omitted_checks"] == ["extra7"]
    assert result["status"] == "complete"
    report = json.loads((project / result["record"]).read_text())
    assert triage.MIN_TAIL_BYTES <= report["tail_bytes"] < triage.TAIL_BYTES


def test_checks_that_do_not_fit_are_unresolved_and_the_rest_sent(project):
    write_json(
        project, "factory.json", config_for(jev={"max_request_bytes": 4096}, extra_checks=failing_checks(4))
    )
    assert not verify_mission(project, "M-ONE", "R-TWO")["pass"]
    provider = Provider("flaky", 0.9)
    result = triage.triage_checks(
        project, "M-ONE", "R-TWO", get_api_key=lambda: "test-key-not-real", request=provider
    )
    sent = json.loads(provider.bodies[0])["state"]["checks"]
    assert 0 < len(sent) < 5 and len(provider.bodies[0].encode()) <= 4096
    reasons = [row.get("reason") for row in result["checks"]]
    assert reasons[len(sent) :] == ["request_too_large"] * (5 - len(sent))
    assert (result["status"], result["reason"]) == ("unresolved", "request_too_large")
    assert "_exit_code" not in result


def test_evidence_or_log_change_during_request_invalidates_answers(project):
    log = project / ".factory/local/runs/M-ONE/R-ONE/unit.stderr.log"
    original = log.read_bytes()

    class Tamper(Provider):
        def __call__(self, body, **options):
            log.write_bytes(original + b"late\n")
            return super().__call__(body, **options)

    result = run(project, Tamper("flaky", 0.9))
    assert (result["status"], result["reason"], result["_exit_code"]) == ("invalid", "inputs_changed", 2)
    assert result["checks"][0]["category"] is None


def test_triage_shares_the_semantic_request_lock(project):
    from software_factory import semantic

    lock = project / semantic.LOCK
    lock.parent.mkdir(parents=True)
    lock.write_text("{}")
    provider = Provider()
    result = run(project, provider)
    assert (result["status"], result["reason"], result["lock"]) == (
        "unavailable",
        "local_request_busy",
        semantic.LOCK,
    )
    assert result["_exit_code"] == 2 and not provider.bodies
    lock.unlink()
    result = run(project, provider)
    assert result["status"] == "complete" and not lock.exists()
    for relative in (semantic.LOCAL, triage.LOCAL):
        assert (project / relative).stat().st_mode & 0o777 == 0o700


def test_cancel_between_log_hashing_steps_stops_before_any_request(project, monkeypatch):
    import threading

    cancel, original = threading.Event(), triage._read_log

    def read_then_cancel(*args, **kwargs):
        value = original(*args, **kwargs)
        cancel.set()
        return value

    monkeypatch.setattr(triage, "_read_log", read_then_cancel)
    provider = Provider()
    result = run(project, provider, cancel=cancel)
    assert (result["status"], result["reason"]) == ("unavailable", "canceled")
    assert not provider.bodies and not list((project / triage.LOCAL).glob("*.json"))
