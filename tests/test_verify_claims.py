"""verify-claims and redaction tests. Every provider call is mocked; no paid inference or network.

As in test_semantic, candidate_snapshot is replaced by a deterministic module stub.
"""

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from software_factory import redaction, semantic
from software_factory.core import FactoryError, asset_root, digest, now, sha256, write_json
from software_factory.jev import JevError
from software_factory.redaction import MASK_PREFIX, bound_text, redact, redact_argv

OPTIONS = tuple(semantic.VERIFY_CRITERIA)
SECRETS = (
    "Zq8vLr3xNw5tKp2m",
    "AKIAIOSFODNN7EXAMPLE",
    "sk-proj-Abcdefghijklmnopqrstuv12",
    "ghp_Abcdefghijklmnopqrstuvwxyz0123456789",
    "xoxb-123456789012-abcdefghijkl",
    "hunter2hunter2",
    "MIIEowIBAAKCAQEA7bq",
)
SECRET_TEXT = """headers = {'Authorization': 'Bearer Zq8vLr3xNw5tKp2m'}
AWS_ACCESS_KEY_ID = AKIAIOSFODNN7EXAMPLE
OPENAI = 'sk-proj-Abcdefghijklmnopqrstuv12'
GITHUB = 'ghp_Abcdefghijklmnopqrstuvwxyz0123456789'
SLACK = 'xoxb-123456789012-abcdefghijkl'
db_password: "hunter2hunter2"
-----BEGIN RSA PRIVATE KEY-----
MIIEowIBAAKCAQEA7bq
-----END RSA PRIVATE KEY-----"""

# Further secret shapes; each value is the fragment that must never leave the machine.
EXTRA_SECRETS = {
    "dsn": "Dsn-pass-9431",
    "stripe": "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc",
    "jwt": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36P",
    "google": "AIza" + "SyD-9tSrke72PouQMnMX-a7eZSW0jkFMBWY",
    "phrase": "horse battery staple",
}
EXTRA_TEXT = f"""DATABASE_URL = "postgres://app:{EXTRA_SECRETS["dsn"]}@db.internal:5432/app"
STRIPE = '{EXTRA_SECRETS["stripe"]}'
session = {EXTRA_SECRETS["jwt"]}
MAPS = "{EXTRA_SECRETS["google"]}"
password = "correct {EXTRA_SECRETS["phrase"]}"
"""


def response_for(body, choices=("supports",), confidence=0.9):
    return {
        "model": body["model"],
        "answers": {
            key: {
                "type": "choice",
                "choice": choices[i % len(choices)],
                "confidence": confidence,
                "probabilities": {
                    option: 0.8 if option == choices[i % len(choices)] else 0.1 for option in OPTIONS
                },
            }
            for i, key in enumerate(body["questions"])
        },
        "usage": {"input_tokens": 50, "output_tokens": 0},
    }


class RedactionTests(unittest.TestCase):
    def test_obvious_secrets_are_masked_and_counted(self):
        masked, count = redact(SECRET_TEXT)
        for secret in SECRETS:
            self.assertNotIn(secret, masked)
        self.assertEqual(count, 7)
        self.assertIn("Authorization': 'Bearer " + MASK_PREFIX, masked)
        self.assertIn("AWS_ACCESS_KEY_ID = " + MASK_PREFIX, masked)
        self.assertIn(MASK_PREFIX + "private_key]", masked)
        for line in ("Authorization: Bearer abcdefgh12345678", "api-key=abc123", "API_KEY: 'value1'"):
            with self.subTest(line=line):
                text, number = redact(line)
                self.assertEqual(number, 1)
                self.assertNotIn(line.rsplit(" ", 1)[-1].split("=")[-1].strip("'"), text)
        unterminated, number = redact("x\n-----BEGIN PRIVATE KEY-----\nabcdef\n")
        self.assertEqual((unterminated, number), ("x\n" + MASK_PREFIX + "private_key]", 1))

    def test_non_secret_text_is_untouched_and_masking_is_idempotent(self):
        for text in (
            "",
            "The token budget rises when passwords rotate.",
            "Use sk- prefixes and the api key documentation; AKIA is a prefix.",
            "def tokenize(value):\n    return value.split()\n",
            'password = ""',
            "Bearer tokens are described in RFC 6750.",
        ):
            with self.subTest(text=text):
                self.assertEqual(redact(text), (text, 0))
        masked, _ = redact(SECRET_TEXT)
        self.assertEqual(redact(masked), (masked, 0))
        with self.assertRaises(TypeError):
            redact(b"bytes")

    def test_home_directory_prefixes_are_masked_and_relative_paths_untouched(self):
        with patch.dict(os.environ, {"HOME": "/srv/builder-home"}):
            for text, expected, number in (
                ("/home/alice/project/app.py:3: error", "~/project/app.py:3: error", 1),
                ("cwd=/Users/bob/work", "cwd=~/work", 1),
                ("C:\\Users\\carol\\repo\\x.py", "~\\repo\\x.py", 1),
                ("/srv/builder-home/.cache/x and /home/dan", "~/.cache/x and ~", 2),
                ('"/home/erin"', '"~"', 1),
            ):
                with self.subTest(text=text):
                    self.assertEqual(redact(text), (expected, number))
                    self.assertEqual(redact(expected), (expected, 0))
            for text in (
                "src/home/alice/app.py",
                "./home/alice/x",
                "tests/Users/bob.py",
                "/usr/lib/python3/home.py",
                "/homeless/x",
                "/home/",
                "/srv/builder-homes/x",
            ):
                with self.subTest(text=text):
                    self.assertEqual(redact(text), (text, 0))
        masked, number = redact("Authorization: Bearer sk-TESTSECRET123456\napi_key=abcd1234efgh5678")
        self.assertNotIn("sk-TESTSECRET123456", masked)
        self.assertNotIn("abcd1234efgh5678", masked)
        self.assertGreaterEqual(number, 2)

    def test_additional_secret_shapes_are_masked_idempotently(self):
        for text, expected in (
            ("https://alice:Pa55word@host/x", "https://alice:[REDACTED:url_password]@host/x"),
            ("postgres://user:hunter2@db:5432/app", "postgres://user:[REDACTED:url_password]@db:5432/app"),
            (
                "mongodb+srv://admin:pw@cluster/db?w=1",
                "mongodb+srv://admin:[REDACTED:url_password]@cluster/db?w=1",
            ),
            ("redis://:pw@host", "redis://:[REDACTED:url_password]@host"),
            ("k=" + "sk_live_abcdefghij1234567890", "k=[REDACTED:stripe_key]"),
            ("sk_test_abcdefghij1234", "[REDACTED:stripe_key]"),
            ("rk_live_abcdefghij1234", "[REDACTED:stripe_key]"),
            ("rk_test_abcdefghij1234", "[REDACTED:stripe_key]"),
            ("t " + EXTRA_SECRETS["jwt"], "t [REDACTED:jwt]"),
            ("key " + EXTRA_SECRETS["google"], "key [REDACTED:google_api_key]"),
            ('password = "correct horse battery staple"', 'password = "[REDACTED:assignment]"'),
            ("db_password: 'two words'", "db_password: '[REDACTED:assignment]'"),
            ('password: str = "hunter2"', 'password: str = "[REDACTED:assignment]"'),
            ('password = f"pw {x}"', 'password = f"[REDACTED:assignment]"'),
            ("'password' => 'a b'", "'password' => '[REDACTED:assignment]'"),
            ("password = other_value", "password = [REDACTED:assignment]"),
            ("pin_token = 12345678901", "pin_token = [REDACTED:assignment]"),
            (
                "https://hooks.slack.com/services/T0000/B0000/XXXXXXXXXXXX",
                "https://hooks.slack.com/services/[REDACTED:slack_webhook]",
            ),
            ("npm_" + "a1" * 18, "[REDACTED:npm_token]"),
            ("mysql -u root -pS3cret app", "mysql -u root -p[REDACTED:password_flag] app"),
            ("tool --password S3cret --verbose", "tool --password [REDACTED:password_flag] --verbose"),
            ("tool --password=S3cret", "tool --password=[REDACTED:password_flag]"),
            ('tool --password "two words"', 'tool --password "[REDACTED:password_flag]"'),
            ("PGPASSWORD=S3cret psql", "PGPASSWORD=[REDACTED:assignment] psql"),
            ("X" * 90 + "_PASSWORD=S3cret", "X" * 90 + "_PASSWORD=[REDACTED:assignment]"),
            (
                "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG",
                "aws_secret_access_key = [REDACTED:assignment]",
            ),
        ):
            with self.subTest(text=text):
                masked, number = redact(text)
                self.assertEqual((masked, number), (expected, 1))
                self.assertEqual(redact(masked), (masked, 0))
        masked, number = redact(EXTRA_TEXT)
        self.assertEqual(number, 5)
        for secret in (*EXTRA_SECRETS.values(), "correct", "horse", "staple"):
            self.assertNotIn(secret, masked)

    def test_ordinary_code_is_not_mangled(self):
        # A public Stripe key, ports and plain code stay as written.
        for text in (
            "if password == other:",
            "if password != other or token <= limit or secret >= 2:",
            "secret_count = compute()",
            "secret_count = compute(a, b)",
            "tokens: list[str]",
            "tokens: list[str] = []",
            "MAX_TOKENS = 4096",
            "temperature_token = 0.5",
            "DEBUG_TOKEN = True",
            "token = None",
            "def check(password: str) -> None:",
            "password = os.environ['PW']",
            "pwd = os.getcwd()",
            "std::token::parse()",
            "pk_live_abcdefghij1234",
            "http://localhost:8080/path",
            "ssh://git@host:22/repo",
            "mysql -u root -p",
        ):
            with self.subTest(text=text):
                self.assertEqual(redact(text), (text, 0))

    def test_argv_masks_values_of_separate_secret_flags(self):
        self.assertEqual(
            redact_argv(
                ["/usr/bin/mysql", "-u", "root", "-pS3cret", "--password", "abc", "--token=x1", "-v", "y"]
            ),
            (
                [
                    "/usr/bin/mysql",
                    "-u",
                    "root",
                    "-p[REDACTED:password_flag]",
                    "--password",
                    "[REDACTED:password_flag]",
                    "--token=[REDACTED:password_flag]",
                    "-v",
                    "y",
                ],
                3,
            ),
        )
        self.assertEqual(
            redact_argv(["pytest", "-pno:cacheprovider", "-q"]), (["pytest", "-pno:cacheprovider", "-q"], 0)
        )

    def test_further_secret_shapes_are_masked_idempotently(self):
        azure = "Zm9vYmFyYmF6cXV4MTIzNDU2Nzg5MA=="
        for text, expected in (
            (
                f"AccountName=acct;AccountKey={azure};EndpointSuffix=x",
                "AccountName=acct;AccountKey=[REDACTED:connection_string];EndpointSuffix=x",
            ),
            (
                "BlobEndpoint=x;SharedAccessSignature=sv=2020-08-04&ss=b&sig=AbCd%2B12",
                "BlobEndpoint=x;SharedAccessSignature=[REDACTED:connection_string]",
            ),
            (
                "https://a.blob.core.windows.net/c?sv=2020-08-04&sig=AbCdEf%2B12345%3D&sp=r",
                "https://a.blob.core.windows.net/c?sv=2020-08-04&sig=[REDACTED:sas_signature]&sp=r",
            ),
            ("curl -u alice:S3cretPw https://h", "curl -u alice:[REDACTED:password_flag] https://h"),
            ("curl --user alice:S3cretPw https://h", "curl --user alice:[REDACTED:password_flag] https://h"),
            ("curl -s --user=alice:S3cretPw h", "curl -s --user=alice:[REDACTED:password_flag] h"),
            ("curl -ualice:S3cretPw h", "curl -ualice:[REDACTED:password_flag] h"),
            ("curl -H 'A: b' -u 'alice:S3cret' h", "curl -H 'A: b' -u 'alice:[REDACTED:password_flag]' h"),
            ("DB_PASS=S3cret", "DB_PASS=[REDACTED:assignment]"),
            ("SMTP_PASS: S3cret", "SMTP_PASS: [REDACTED:assignment]"),
            ("PASS=S3cret", "PASS=[REDACTED:assignment]"),
            ("export PASS_FILE=S3cret", "export PASS_FILE=[REDACTED:assignment]"),
            ("pass: S3cret", "pass: [REDACTED:assignment]"),
            ("model hf_" + "a1B2c3D4" * 4, "model [REDACTED:huggingface_token]"),
            ("t=glpat-" + "xYz12AbC3dEf4GhI5jKl", "t=[REDACTED:gitlab_token]"),
            ("SG." + "aB3" * 8 + "." + "cD4-" * 10, "[REDACTED:sendgrid_key]"),
            (
                "Authorization: Bot "
                + "MTk4NjIyNDgzNDcxOTI1MjQ4"
                + "."
                + "Cl2FMQ"
                + "."
                + "ZnCjm1XVW7vRze4b7Cq4se7kKWs",
                "Authorization: Bot [REDACTED:authorization]",
            ),
            ('{"auth": "dXNlcjpwYXNzd29yZA=="}', '{"auth": "[REDACTED:auth_value]"}'),
            ("_auth=dXNlcjpwYXNz", "_auth=[REDACTED:auth_value]"),
            (
                "machine example.com login alice password S3cretPw",
                "machine example.com login alice password [REDACTED:netrc_password]",
            ),
            ("machine h\n  password S3cretPw", "machine h\n  password [REDACTED:netrc_password]"),
            ("sshpass -p S3cret ssh host", "sshpass -p [REDACTED:password_flag] ssh host"),
            (
                "docker login -u bob -p S3cret registry.example",
                "docker login -u bob -p [REDACTED:password_flag] registry.example",
            ),
        ):
            with self.subTest(text=text):
                masked, number = redact(text)
                self.assertEqual((masked, number), (expected, 1))
                self.assertEqual(redact(masked), (masked, 0))
        for text in (
            "bypass = S3cret",
            "passed = S3cret",
            "compass = north",
            "test_passes = run_all",
            "x.pass_rate = compute_rate",
            "PASS: tests/test_x.py",
            "if x: pass",
            "password is required",
            "The password S3cret was rotated",
            "curl -u alice https://h",
            "mysql -p dbname",
            "hf_short",
            "glpat-short",
        ):
            with self.subTest(text=text):
                self.assertEqual(redact(text), (text, 0))

    def test_argv_masks_curl_sshpass_and_docker_login_passwords(self):
        mask = "[REDACTED:password_flag]"
        for argv, expected in (
            (["curl", "-u", "alice:S3cret", "h"], ["curl", "-u", "alice:" + mask, "h"]),
            (["/usr/bin/curl", "--user", "alice:S3:cret"], ["/usr/bin/curl", "--user", "alice:" + mask]),
            (["curl", "-ualice:S3cret"], ["curl", "-ualice:" + mask]),
            (["curl", "--user=alice:S3cret"], ["curl", "--user=alice:" + mask]),
            (["sshpass", "-p", "S3cret", "ssh", "h"], ["sshpass", "-p", mask, "ssh", "h"]),
            (["sshpass", "-pS3cret", "ssh"], ["sshpass", "-p" + mask, "ssh"]),
            (["docker", "login", "-p", "S3cret", "r"], ["docker", "login", "-p", mask, "r"]),
            (["docker", "login", "--password", "S3cret"], ["docker", "login", "--password", mask]),
        ):
            with self.subTest(argv=argv):
                self.assertEqual(redact_argv(argv), (expected, 1))
        for argv in (
            ["curl", "-u", "alice", "h"],
            ["mysql", "-p", "dbname"],
            ["docker", "run", "-p", "8080:80", "img"],
            ["tool", "-p", "x"],
            ["tool", "-u", "alice:bob"],
        ):
            with self.subTest(argv=argv):
                self.assertEqual(redact_argv(argv), (argv, 0))

    def test_every_pattern_is_linear_on_adversarial_input(self):
        size = 64 * 1024
        seeds = (
            "--password-", "--secret-", "password", "password: a | ", "api_key", "DB_PASS", "pass_",
            "eyJ-", "sk-", "sk-a-", "hf_a", "SG.", "glpat-", "mysql ", "curl ", "curl -u a", "docker login ",
            "sshpass -p", "a://x:", "authorization:", "bearer ", "AccountKey=", "?sig=", "login a password ",
            "\"auth\": \"", "_auth=", "-----BEGIN ", "PRIVATE ", "/home/", "'", '"', ":", "@", "a", "-",
        )  # fmt: skip
        slowest = 0.0
        for seed in seeds:
            for filler in ("", " ", "a", ":", "'"):
                half = (seed * (size // len(seed) + 1))[: size if not filler else size // 2]
                text = half + filler * (size - len(half))
                for kind, pattern, _ in redaction._PATTERNS:
                    start = time.perf_counter()
                    pattern.sub("", text)
                    elapsed = time.perf_counter() - start
                    slowest = max(slowest, elapsed)
                    self.assertLess(elapsed, 2.0, (kind, seed, filler))
        start = time.perf_counter()
        redact_argv(["curl", "--secret-" * 8000, "-u", "a:" * 32000, "--password-" * 6000, "x"])
        self.assertLess(time.perf_counter() - start, 2.0)
        self.assertLess(slowest, 2.0)

    def test_line_bounded_cut_never_splits_a_secret_or_a_mask(self):
        secret = 'password = "correct horse battery staple"\n'
        raw = "a\n" * 10 + secret + "tail\n"
        # Cut inside the secret line: the partial line is dropped before masking.
        head, cut = bound_text(raw, 20 + 20)
        self.assertTrue(cut)
        self.assertEqual(head, "a\n" * 10)
        tail, cut = bound_text(raw, len("tail\n") + 12, tail=True)
        self.assertEqual((tail, cut), ("tail\n", True))
        masked, _ = redact(raw)
        for limit in range(len(masked.encode()) + 1):
            for from_end in (False, True):
                with self.subTest(limit=limit, tail=from_end):
                    text, _ = bound_text(masked, limit, tail=from_end)
                    self.assertLessEqual(len(text.encode()), limit)
                    self.assertTrue(masked.startswith(text) if not from_end else masked.endswith(text))
                    self.assertEqual(text.count("[REDACTED:"), text.count("[REDACTED:assignment]"))
                    self.assertNotIn("horse", text)
        self.assertEqual(bound_text("short", 10), ("short", False))
        self.assertEqual(bound_text("\u00e9" * 10, 5), ("", True))


class VerifyClaimsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="factory-verify-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        shutil.copytree(asset_root(), self.root / ".factory")
        self.config = {
            "schema_version": 1,
            "name": "verify-fixture",
            "profile": "codex",
            "completion_target": "READY_PR",
            "work_types": ["maintenance"],
            "limits": {"repair_attempts": 3, "parallel_writers": 1, "check_timeout_seconds": 30},
            "checks": [
                {
                    "id": "unit",
                    "command": [sys.executable, "-c", "pass"],
                    "cwd": ".",
                    "required": True,
                    "timeout_seconds": 30,
                }
            ],
            "owners": {"maintainer": None, "reviewer": None},
            "delivery": {
                "enabled": False,
                "staging_command": None,
                "release_command": None,
                "recovery_command": None,
            },
            "evidence_exclude": [".factory/local/", ".factory/missions/"],
            "jev": {
                "enabled": True,
                "provider": "typesafe",
                "model": "jev-1.13.0",
                "claim_mode": "advisory",
            },
        }
        self.save_config()
        (self.root / ".gitignore").write_text(".factory/local/\nbuild/\n")
        (self.root / "src").mkdir()
        (self.root / "src/app.py").write_text("def greet():\n    return 'hello'\n")
        (self.root / "src/stable.py").write_text("VALUE = 1\n")
        (self.root / "src/secrets.py").write_text("SETTING = 1\n")
        self.git("init", "-q")
        self.git("add", ".")
        self.git(
            "-c",
            "user.name=Factory Test",
            "-c",
            "user.email=factory@example.invalid",
            "commit",
            "-qm",
            "baseline",
        )
        self.base = self.git("rev-parse", "HEAD")
        self.mission = {
            "schema_version": 1,
            "id": "M-0001",
            "title": "Verify fixture",
            "kind": "maintenance",
            "state": "IMPLEMENTING",
            "created_at": now(),
            "updated_at": now(),
            "profile": "codex",
            "base_commit": self.base,
            "branch": "main",
            "spec_hash": None,
            "constitution_hash": "0" * 64,
            "tasks": [
                {
                    "id": "T1",
                    "title": "Greeting",
                    "status": "RUNNING",
                    "depends_on": [],
                    "owned_paths": ["src/"],
                    "checks": ["unit"],
                    "attempts": 1,
                }
            ],
            "decisions": [],
            "evidence": [],
            "reviews": [],
            "blockers": [],
        }
        write_json(self.root, ".factory/missions/M-0001/mission.json", self.mission)
        (self.root / "src/app.py").write_text("def greet(name):\n    return f'hello {name}'\n")
        stub = types.ModuleType("software_factory.evidence")
        stub.candidate_snapshot = self.snapshot
        replacement = patch.dict(sys.modules, {"software_factory.evidence": stub})
        replacement.start()
        self.addCleanup(replacement.stop)
        self.calls = []

    def git(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            check=True,
            env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
        ).stdout.strip()

    def snapshot(self, root, base=None):
        values = {}
        for path in Path(root).rglob("*"):
            relative = path.relative_to(root)
            if (
                path.is_file()
                and not path.is_symlink()
                and relative.parts[0] != ".git"
                and relative.parts[:2] != (".factory", "local")
            ):
                values[str(relative)] = sha256(path.read_bytes())
        return {"fingerprint": digest(values), "head": self.git("rev-parse", "HEAD")}

    def save_config(self):
        write_json(self.root, "factory.json", self.config)

    def setting(self, **values):
        self.config["jev"].update(values)
        self.save_config()

    def claims(self, *items):
        return {
            "schema_version": 1,
            "claims": [
                {"id": f"C{index + 1}", "claim": text, "paths": list(paths)}
                for index, (text, paths) in enumerate(items or [("greet takes a name", ["src/app.py"])])
            ],
        }

    def responder(self, choices=("supports",), confidence=0.9):
        def request(body, **options):
            self.calls.append(copy.deepcopy(body))
            return response_for(body, choices, confidence)

        return request

    def run_verify(self, value=None, **options):
        args = {
            "mission": "M-0001",
            "input_data": self.claims() if value is None else value,
            "get_api_key": lambda: "mock-not-a-secret",
            "request": self.responder(),
        }
        args.update(options)
        return semantic.verify_claims(self.root, **args)

    def never(self, *args, **kwargs):
        raise AssertionError("provider request")

    def records(self, category):
        return list((self.root / semantic.VERIFY_LOCAL / category).glob("*.json"))

    def test_saved_credential_and_store_failure(self):
        from software_factory import auth

        key = "synthetic-verifyclaimstests-stored-key"
        auth.save_typesafe_key(key)

        def request(body, **options):
            self.assertEqual(options["api_key"], key)
            return self.responder()(body, **options)

        result = self.run_verify(get_api_key=None, request=request, no_cache=True)
        self.assertTrue(self.calls)
        self.assertNotIn(key, json.dumps(result))
        (auth._directory() / auth.FILENAME).write_text("broken-store")
        result = self.run_verify(get_api_key=None, request=request, no_cache=True)
        self.assertEqual(result["reason"], "credential_unavailable")

    def test_unchanged_path_is_no_evidence_without_request(self):
        result = self.run_verify(
            self.claims(("stable value changed", ["src/stable.py"])),
            request=self.never,
            get_api_key=lambda: self.fail("credential lookup"),
        )
        self.assertEqual(result["status"], "complete")
        row = result["claims"][0]
        self.assertEqual(
            (row["verdict"], row["reason"], row["review"]), ("no_evidence", "no_changes_since_base", None)
        )
        self.assertEqual(
            row["evidence"], [{"path": "src/stable.py", "changed": False, "sha256": None, "truncated": False}]
        )
        self.assertIsNone(result["usage"])
        self.assertFalse(self.records("cache"))

    def test_verdicts_follow_choice_and_confidence_threshold(self):
        for choice, confidence, review in (
            ("supports", 0.9, "accepted"),
            ("contradicts", 0.95, "accepted"),
            ("says_nothing", 0.6, "needs_review"),
        ):
            with self.subTest(choice=choice):
                self.calls.clear()
                result = self.run_verify(request=self.responder((choice,), confidence), no_cache=True)
                self.assertEqual(result["status"], "complete")
                row = result["claims"][0]
                self.assertEqual(
                    (row["verdict"], row["review"], row["confidence"]), (choice, review, confidence)
                )
                self.assertEqual(row["paths"], ["src/app.py"])
                self.assertFalse(row["truncated"])
                self.assertEqual(len(self.calls), 1)
                body = self.calls[0]
                self.assertEqual(set(body["questions"]["q0"]["criteria"]), set(OPTIONS))
                self.assertIn("hello {name}", body["state"]["claims"][0]["changes"][0]["diff"])
                record = json.loads((self.root / result["record"]).read_text())
                # Reports keep excerpt hashes, never excerpt text.
                self.assertNotIn("hello {name}", json.dumps(record))
                self.assertEqual(
                    record["claims"][0]["evidence"][0]["sha256"],
                    sha256(body["state"]["claims"][0]["changes"][0]["diff"]),
                )

    def test_mixed_claims_send_only_claims_with_evidence(self):
        (self.root / "src/new_module.py").write_text("NEW = True\n")
        value = self.claims(
            ("stable changed", ["src/stable.py"]),
            ("greet takes a name", ["src/app.py", "src/stable.py"]),
            ("a new module exists", ["src/new_module.py"]),
        )
        value["claims"][1]["task_id"] = "T1"
        result = self.run_verify(value, request=self.responder(("supports", "contradicts")))
        self.assertEqual(
            [row["verdict"] for row in result["claims"]], ["no_evidence", "supports", "contradicts"]
        )
        self.assertEqual(result["claims"][1]["task_id"], "T1")
        body = self.calls[0]
        self.assertEqual([entry["id"] for entry in body["state"]["claims"]], ["C2", "C3"])
        self.assertEqual([change["path"] for change in body["state"]["claims"][0]["changes"]], ["src/app.py"])
        self.assertIn("+NEW = True", body["state"]["claims"][1]["changes"][0]["diff"])
        self.assertIn('"C3"', body["questions"]["q1"]["instructions"])
        self.assertEqual(result["review"]["accepted"], 2)

    def test_secrets_in_diff_are_masked_before_sending_and_never_stored(self):
        (self.root / "src/secrets.py").write_text("SETTING = 1\n" + SECRET_TEXT + "\n")
        value = self.claims(("configured client; api_key=Zq8vLr3xNw5tKp2m", ["src/secrets.py"]))
        result = self.run_verify(value)
        sent = json.dumps(self.calls[0])
        for secret in SECRETS:
            self.assertNotIn(secret, sent)
        self.assertIn(MASK_PREFIX, sent)
        self.assertEqual(result["claims"][0]["masks"], 8)
        stored = "".join(path.read_text() for path in (self.root / semantic.LOCAL).rglob("*.json"))
        for secret in SECRETS:
            self.assertNotIn(secret, stored)
            self.assertNotIn(secret, json.dumps(result))

    def test_additional_secret_shapes_never_reach_the_provider(self):
        (self.root / "src/secrets.py").write_text("SETTING = 1\n" + EXTRA_TEXT)
        value = self.claims(
            (f"uses {EXTRA_SECRETS['stripe']} and password='a {EXTRA_SECRETS['phrase']}'", ["src/secrets.py"])
        )
        result = self.run_verify(value)
        sent = json.dumps(self.calls[0])
        for secret in (*EXTRA_SECRETS.values(), "horse", "staple"):
            self.assertNotIn(secret, sent)
        self.assertIn(MASK_PREFIX + "url_password]", sent)
        self.assertEqual(result["claims"][0]["masks"], 7)

    def test_secret_straddling_the_raw_read_bound_is_never_sent_as_a_fragment(self):
        line = "charge with " + EXTRA_SECRETS["stripe"] + "\n"
        # The raw read bound falls three characters after "sk_live_", where a byte cut would
        # leave a fragment too short to recognize.
        prefix = semantic.VERIFY_RAW_BYTES - len("charge with sk_live_4eC")
        head = ("x" * 99 + "\n") * (prefix // 100 - 1)
        head += "#" * (prefix - len(head) - 1) + "\n"
        self.assertEqual(len(head), prefix)
        (self.root / "src/big.py").write_text(head + line + "y\n" * 100)
        diff = semantic._path_diff(self.root, "C1", "src/big.py", self.base)
        self.assertNotIn("sk_live", diff)
        self.assertNotIn("4eC", diff)
        self.assertEqual(diff.splitlines()[-1], "+" + head.rsplit("\n", 2)[-2])
        result = self.run_verify(self.claims(("big file added", ["src/big.py"])))
        self.assertTrue(result["claims"][0]["truncated"])
        self.assertNotIn("sk_live", json.dumps(self.calls[0]))

    def test_mixed_case_private_paths_and_private_aliases_are_rejected(self):
        for relative in (".GIT/config", ".Git/HEAD", ".factory/LOCAL/notes.txt", ".FACTORY/Local/x.txt"):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("PRIVATE = 1\n")
            with self.subTest(path=relative):
                value = self.claims(("claim text", [relative]))
                with self.assertRaisesRegex(FactoryError, "repository metadata or private local records"):
                    self.run_verify(value, request=self.never, no_persist=True)
        # A filesystem that aliases names (case-insensitive, normalization) resolves src to .git here.
        real = os.path.samefile

        def aliased(first, second):
            if os.path.realpath(first) == os.path.realpath(self.root / "src") and Path(second).name == ".git":
                return True
            return real(first, second)

        with (
            patch.object(semantic.os.path, "samefile", side_effect=aliased),
            self.assertRaisesRegex(FactoryError, "repository metadata or private local records"),
        ):
            self.run_verify(request=self.never, no_persist=True)
        self.assertTrue(self.run_verify(no_persist=True)["claims"])

    def test_unsafe_private_symlinked_or_missing_paths_are_rejected(self):
        directory = tempfile.TemporaryDirectory(prefix="factory-outside-")
        self.addCleanup(directory.cleanup)
        outside = Path(directory.name) / "outside.py"
        outside.write_text("OUTSIDE = 1\n")
        (self.root / "src/link.py").symlink_to(outside)
        (self.root / "linked").symlink_to(self.root / "src", target_is_directory=True)
        (self.root / ".factory/local").mkdir(parents=True, exist_ok=True)
        (self.root / ".factory/local/notes.txt").write_text("private")
        (self.root / "build").mkdir()
        (self.root / "build/out.txt").write_text("generated")
        for path in (
            "../outside.py",
            str(outside),
            "src/../src/app.py",
            ".factory/local/notes.txt",
            ".git/config",
            "src/link.py",
            "linked/app.py",
            "src/missing.py",
            "src",
            "build/out.txt",
        ):
            with self.subTest(path=path):
                value = self.claims(("claim text", ["src/app.py", path]))
                with self.assertRaisesRegex(FactoryError, "Claim C1 cites a path") as caught:
                    self.run_verify(value, request=self.never, no_persist=True)
                self.assertNotIn(path, str(caught.exception))
        self.assertFalse(self.records("advisory"))

    def test_shadow_mode_leaks_no_judgment(self):
        self.setting(claim_mode="shadow")
        outputs = {}
        for choice, confidence in (("supports", 0.95), ("contradicts", 0.6)):
            result = self.run_verify(request=self.responder((choice,), confidence), no_cache=True)
            hidden = json.loads((self.root / result["report"]).read_text())
            self.assertEqual(hidden["claims"][0]["verdict"], choice)
            self.assertEqual(result["report_hash"], digest(hidden))
            self.assertTrue(result["judgments_withheld"])
            text = json.dumps(result)
            for leaked in ("verdict", "review", "accepted", "confidence", "supports", "contradicts", "usage"):
                self.assertNotIn(leaked, text)
            outputs[choice] = {
                key: item
                for key, item in result.items()
                if key not in ("report", "report_hash", "elapsed_ms")
            }
        self.assertEqual(outputs["supports"], outputs["contradicts"])
        self.assertEqual(outputs["supports"]["status"], "complete")
        skipped = self.run_verify(request=self.never, no_persist=True)
        self.assertEqual((skipped["status"], skipped["reason"]), ("skipped", "shadow_requires_local_record"))

    def test_disabled_makes_no_request_and_touches_nothing(self):
        self.setting(enabled=False)
        with patch.object(semantic, "_verify_context", side_effect=AssertionError("candidate read")):
            result = semantic.verify_claims(
                self.root,
                mission="M-0001",
                input_path="../never-read",
                get_api_key=lambda: self.fail("credential lookup"),
                request=self.never,
            )
        self.assertEqual((result["status"], result["reason"]), ("unavailable", "disabled"))
        self.assertFalse((self.root / semantic.LOCAL).exists())

    def test_request_too_large_leaves_later_claims_unresolved(self):
        (self.root / "src/stable.py").write_text(
            "VALUE = 1\n" + "".join(f"LINE_{n} = {n}\n" for n in range(600))
        )
        value = self.claims(("greet takes a name", ["src/app.py"]), ("stable grew", ["src/stable.py"]))
        self.setting(max_request_bytes=3000)
        result = self.run_verify(value)
        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(result["reason"], "request_too_large")
        first, second = result["claims"]
        self.assertEqual(first["verdict"], "supports")
        self.assertEqual(
            (second["verdict"], second["reason"], second["review"]), ("unresolved", "request_too_large", None)
        )
        self.assertTrue(second["truncated"])
        self.assertEqual(len(self.calls[0]["questions"]), 1)
        self.assertLessEqual(len(json.dumps(self.calls[0], separators=(",", ":")).encode()), 3000)
        self.setting(max_request_bytes=1024)
        self.calls.clear()
        result = self.run_verify(value, request=self.never)
        self.assertEqual([row["reason"] for row in result["claims"]], ["request_too_large"] * 2)
        self.assertIsNone(result["usage"])

    def test_diffs_are_bounded_per_path_and_claim(self):
        for index in range(5):
            (self.root / f"src/big{index}.py").write_text("".join(f"ITEM_{n} = {n}\n" for n in range(900)))
        value = self.claims(("many modules", [f"src/big{index}.py" for index in range(5)]))
        result = self.run_verify(value)
        changes = self.calls[0]["state"]["claims"][0]["changes"]
        sizes = [len(change["diff"].encode()) for change in changes]
        self.assertTrue(all(size <= semantic.VERIFY_PATH_BYTES for size in sizes))
        self.assertLessEqual(sum(sizes), semantic.VERIFY_CLAIM_BYTES)
        self.assertEqual(len(changes), 4)
        self.assertTrue(result["claims"][0]["truncated"])
        self.assertEqual([item["sent"] for item in result["claims"][0]["evidence"]], [True] * 4 + [False])

    def test_cache_hit_and_miss_on_threshold_change(self):
        request = self.responder(confidence=0.7)
        first = self.run_verify(request=request)
        self.assertEqual(first["claims"][0]["review"], "needs_review")
        self.assertNotIn("review", json.dumps(json.loads(self.records("cache")[0].read_text())))
        hit = self.run_verify(request=request, get_api_key=lambda: self.fail("key on cache hit"))
        self.assertTrue(hit["cache_hit"])
        self.assertEqual(hit["claims"][0]["review"], "needs_review")
        self.setting(claim_accept_confidence=0.6)
        changed = self.run_verify(request=request)
        self.assertFalse(changed["cache_hit"])
        self.assertEqual(changed["claims"][0]["review"], "accepted")
        self.assertEqual(changed["provenance"]["claim_accept_confidence"], 0.6)
        self.assertEqual(len(self.calls), 2)
        # A changed candidate diff is also a miss.
        (self.root / "src/app.py").write_text("def greet(name):\n    return f'hi {name}'\n")
        self.assertFalse(self.run_verify(request=request)["cache_hit"])
        self.assertEqual(len(self.calls), 3)
        self.assertFalse(self.run_verify(request=request, no_cache=True)["cache_hit"])
        self.assertEqual(len(self.calls), 4)

    def test_provider_failures_no_network_and_missing_credentials_are_nonjudgments(self):
        def fail(*args, **kwargs):
            raise JevError("http_503")

        for options, reason, status in (
            ({"request": fail}, "http_503", "unavailable"),
            ({"get_api_key": lambda: None}, "credential_missing", "unavailable"),
            ({"request": lambda body, **_: {"model": body["model"]}}, "invalid_response", "invalid"),
            ({"no_network": True, "request": self.never}, "network_disallowed", "unresolved"),
        ):
            with self.subTest(reason=reason):
                value = self.claims(("greet", ["src/app.py"]), ("stable", ["src/stable.py"]))
                result = self.run_verify(value, no_cache=True, **options)
                self.assertEqual((result["status"], result["reason"]), (status, reason))
                self.assertEqual(
                    [(row["verdict"], row["review"]) for row in result["claims"]],
                    [("unresolved", None), ("no_evidence", None)],
                )

    def test_abstain_answer_needs_review(self):
        row = {"status": "pending"}
        semantic._verify_answer(
            row, {"choice": "abstain", "confidence": 0.99, "probabilities": {"abstain": 0.99}}, 0.8
        )
        self.assertEqual(
            (row["verdict"], row["reason"], row["review"]), ("unresolved", "abstained", "needs_review")
        )

    def test_never_modifies_mission_records_or_candidate(self):
        before = {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and ".git" not in path.parts
        }
        self.run_verify()
        after = {
            str(path.relative_to(self.root)): path.read_bytes()
            for path in self.root.rglob("*")
            if path.is_file() and ".git" not in path.parts and "local" not in path.parts
        }
        self.assertEqual(before, after)

    def test_input_validation_is_generic_and_task_ids_are_checked(self):
        secret_claim = "leaky-claim-text " * 100
        for value, message in (
            (
                {
                    "schema_version": 1,
                    "claims": [{"id": "C1", "claim": secret_claim, "paths": ["src/app.py"]}],
                },
                "Invalid verify-claims input",
            ),
            ({"schema_version": 1, "claims": []}, "Invalid verify-claims input"),
            (
                {"schema_version": 1, "claims": [{"id": "C1", "claim": "x", "paths": []}]},
                "Invalid verify-claims input",
            ),
            (self.claims(("a", ["src/app.py"]), ("b", ["src/app.py"])) | {}, None),
        ):
            if message is None:
                value["claims"][1]["id"] = "C1"
                message = "Duplicate verify-claims claim ID"
            with self.subTest(message=message), self.assertRaisesRegex(FactoryError, message) as caught:
                self.run_verify(value, request=self.never, no_persist=True)
            self.assertNotIn("leaky-claim-text", str(caught.exception))
        value = self.claims()
        value["claims"][0]["task_id"] = "T9"
        with self.assertRaisesRegex(FactoryError, "Claim C1 names a task that is not in the mission"):
            self.run_verify(value, request=self.never, no_persist=True)
        self.setting(max_pairs=1)
        with self.assertRaisesRegex(FactoryError, "claim count exceeds"):
            self.run_verify(self.claims(("a", ["src/app.py"]), ("b", ["src/app.py"])), request=self.never)
        with self.assertRaises(FactoryError):
            self.run_verify(mission="M-9999", request=self.never, no_persist=True)

    def test_parser_input_safety_and_exit_codes(self):
        parser = argparse.ArgumentParser()
        parser.set_defaults(root=self.root)
        semantic.add_parser(parser.add_subparsers())
        with self.assertRaises(SystemExit), patch("sys.stderr"):
            parser.parse_args(["semantic", "verify-claims", "--input", ".factory/local/claims.json"])
        write_json(self.root, ".factory/local/claims.json", self.claims())
        argv = ["jev", "verify-claims", "--mission", "M-0001", "--input", ".factory/local/claims.json"]
        args = parser.parse_args([*argv, "--no-persist"])
        with patch.dict(
            os.environ, {name: os.environ[name] for name in ("XDG_CONFIG_HOME", "APPDATA")}, clear=True
        ):
            result = args.handler(args)
        self.assertEqual((result["reason"], result["_exit_code"]), ("credential_missing", 2))
        args = parser.parse_args([*argv, "--no-network"])
        result = args.handler(args)
        self.assertEqual(result["status"], "unresolved")
        self.assertNotIn("_exit_code", result)
        (self.root / ".factory/local/link.json").symlink_to(self.root / ".factory/local/claims.json")
        (self.root / ".factory/local/big.json").write_bytes(b" " * 262145)
        for relative in (".factory/local/link.json", ".factory/local/big.json", "../claims.json"):
            with self.subTest(relative=relative), self.assertRaises(FactoryError):
                args = parser.parse_args(
                    ["semantic", "verify-claims", "--mission", "M-0001", "--input", relative]
                )
                args.handler(args)
        self.setting(enabled=False)
        result = args.handler(args)
        self.assertEqual((result["status"], result["_exit_code"]), ("unavailable", 2))


if __name__ == "__main__":
    unittest.main()
