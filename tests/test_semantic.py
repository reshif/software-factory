"""Semantic boundary tests. All provider calls are mocked, with no paid inference.

The candidate-snapshot dependency is supplied through a temporary module stub so
these tests verify semantic revalidation independently of the evidence module.
"""

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from software_factory import semantic
from software_factory.core import FactoryError, asset_root, digest, now, sha256, write_json
from software_factory.jev import RELATIONS, JevError


def response_for(body, choices=("supports",), confidence=0.91):
    return {
        "model": body["model"],
        "answers": {
            key: {
                "type": "choice",
                "choice": choices[i % len(choices)],
                "confidence": confidence,
                "probabilities": {
                    relation: 0.8 if relation == choices[i % len(choices)] else 0.05 for relation in RELATIONS
                },
            }
            for i, key in enumerate(body["questions"])
        },
        "usage": {"input_tokens": 123, "output_tokens": 0},
    }


def packet():
    value = semantic.example()
    value["claims"] = value["claims"][:1]
    return value


class SemanticTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="factory-semantic-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        shutil.copytree(asset_root(), self.root / ".factory")
        # Mirror the optional jev.claim_accept_confidence configuration property
        # (number 0-1) in the fixture schema when the packaged schema lacks it.
        factory_schema = self.root / ".factory/schemas/factory.schema.json"
        schema = json.loads(factory_schema.read_text())
        schema["properties"]["jev"]["properties"].setdefault(
            "claim_accept_confidence", {"type": "number", "minimum": 0, "maximum": 1}
        )
        factory_schema.write_text(json.dumps(schema, indent=2) + "\n")
        self.config = {
            "schema_version": 1,
            "name": "semantic-fixture",
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
        (self.root / ".gitignore").write_text(".factory/local/\n")
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
        stub = types.ModuleType("software_factory.evidence")
        stub.candidate_snapshot = self.snapshot
        replacement = patch.dict(sys.modules, {"software_factory.evidence": stub})
        replacement.start()
        self.addCleanup(replacement.stop)
        self.calls = []

    def git(self, *args):
        result = subprocess.run(
            ["git", "-C", str(self.root), *args],
            capture_output=True,
            text=True,
            check=True,
            env={key: value for key, value in os.environ.items() if not key.startswith("GIT_")},
        )
        return result.stdout.strip()

    def snapshot(self, root, base=None):
        # Deliberately deterministic stand-in for candidate_snapshot. Private
        # records are excluded just as required by the production contract.
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

    def request(self, body, **options):
        self.calls.append(copy.deepcopy(body))
        return response_for(body)

    def run_packet(self, value=None, **options):
        args = {
            "input_data": packet() if value is None else value,
            "get_api_key": lambda: "mock-not-a-secret",
            "request": self.request,
        }
        args.update(options)
        return semantic.evaluate_claims(self.root, **args)

    def files(self, category):
        return list((self.root / semantic.LOCAL / category).glob("*.json"))

    def test_saved_credential_and_store_failure(self):
        from software_factory import auth

        key = "synthetic-semantictests-stored-key"
        auth.save_typesafe_key(key)

        def request(body, **options):
            self.assertEqual(options["api_key"], key)
            return self.request(body, **options)

        result = self.run_packet(get_api_key=None, request=request, no_cache=True)
        self.assertTrue(self.calls)
        self.assertNotIn(key, json.dumps(result))
        (auth._directory() / auth.FILENAME).write_text("broken-store")
        result = self.run_packet(get_api_key=None, request=request, no_cache=True)
        self.assertEqual(result["reason"], "credential_unavailable")

    def test_off_no_network_and_status_never_touch_input_keys_candidate_or_records(self):
        for state in ("off", "omitted", "no-network"):
            with self.subTest(state=state):
                if state == "omitted":
                    self.config.pop("jev", None)
                else:
                    self.config["jev"] = {
                        "enabled": state != "off",
                        "provider": "typesafe",
                        "model": "jev-1.13.0",
                    }
                self.save_config()
                with patch.object(semantic, "_context", side_effect=AssertionError("candidate read")):
                    result = semantic.evaluate_claims(
                        self.root,
                        input_path="../never-read",
                        no_network=state == "no-network",
                        get_api_key=lambda: self.fail("credential lookup"),
                        request=lambda *_: self.fail("request"),
                    )
                self.assertEqual(result["status"], "skipped")
                self.assertIsNone(result["coverage"])
                self.assertEqual(semantic.semantic_status(self.root)["credential"], "not_checked")
                self.assertFalse((self.root / semantic.LOCAL).exists())

    def test_config_rejects_unknown_operations_aliases_modes_and_unbounded_limits(self):
        base = copy.deepcopy(self.config)
        for change in (
            {"operations": ["model_dispatch"]},
            {"model": "jev-latest"},
            {"claim_mode": "approve"},
            {"deadline_ms": 0},
            {"max_pairs": 256},
            {"max_request_bytes": 1000000},
            {"endpoint": "https://evil.invalid"},
            {"claim_accept_confidence": 1.5},
            {"claim_accept_confidence": -0.1},
            {"claim_accept_confidence": "0.8"},
        ):
            with self.subTest(change=change), self.assertRaises(FactoryError):
                self.config = copy.deepcopy(base)
                self.setting(**change)
                semantic.semantic_status(self.root)

    def test_association_of_all_five_relationships_and_private_record_redaction(self):
        value = packet()
        base = value["sources"][0]
        value["claims"] = [
            {"id": "C" + str(i), "text": "Atomic private claim " + str(i), "source_ids": ["S" + str(i)]}
            for i in range(5)
        ]
        value["sources"] = [
            {
                **base,
                "id": "S" + str(i),
                "excerpt": "Distinct private excerpt " + str(i),
                "sha256": sha256("Distinct private excerpt " + str(i)),
            }
            for i in range(5)
        ]

        def request(body, **kwargs):
            self.assertEqual(len(body["state"]["pairs"]), 5)
            for i in range(5):
                self.assertIn(f"state.pairs[{i}].sources", body["questions"]["q" + str(i)]["instructions"])
                self.assertEqual(body["state"]["pairs"][i]["sources"][0]["id"], "S" + str(i))
                self.assertNotIn("reference", body["state"]["pairs"][i]["sources"][0])
            return response_for(body, RELATIONS)

        result = self.run_packet(value, request=request)
        self.assertEqual([row["relation"] for row in result["claims"]], list(RELATIONS))
        self.assertEqual(result["coverage"]["evaluated"], 5)
        self.assertTrue(result["advisory_only"])
        text = (self.root / result["record"]).read_text()
        for secret in ("Atomic private", "Distinct private", "mock-not-a-secret"):
            self.assertNotIn(secret, text)
        self.assertEqual((self.root / result["record"]).stat().st_mode & 0o777, 0o600)

    def test_invalid_hash_ids_references_dates_and_count_fail_before_keys(self):
        mutators = [
            lambda v: v["sources"][0].update(sha256="0" * 64),
            lambda v: v["sources"].append(v["sources"][0]),
            lambda v: v["claims"].append(v["claims"][0]),
            lambda v: v["claims"][0].update(source_ids=["absent"]),
            lambda v: v["sources"][0].update(retrieved_at="2026-02-30T00:00:00Z"),
            lambda v: v["sources"][0].update(retrieved_at="2999-01-01T00:00:00Z"),
            lambda v: v.update(claims=[{**v["claims"][0], "id": "C" + str(i)} for i in range(17)]),
        ]
        for mutate in mutators:
            with self.subTest(mutate=mutate), self.assertRaises(FactoryError):
                value = packet()
                mutate(value)
                self.run_packet(value, get_api_key=lambda: self.fail("key lookup"))
        self.assertEqual(self.calls, [])

    def test_blank_or_unknown_input_fields_are_sanitized(self):
        for collection, field in (
            ("sources", "reference"),
            ("sources", "locator"),
            ("sources", "excerpt"),
            ("claims", "text"),
            ("claims", "quote"),
        ):
            with self.subTest(field=field), self.assertRaisesRegex(FactoryError, "Invalid semantic input"):
                value = packet()
                value[collection][0][field] = " \t\n"
                self.run_packet(value, get_api_key=lambda: self.fail("key lookup"))
        value = packet()
        value["claims"][0]["private_extra"] = "SECRET request body"
        with self.assertRaises(FactoryError) as error:
            self.run_packet(value)
        self.assertNotIn("SECRET", str(error.exception))

    def test_quote_freshness_and_complete_context_preserve_all_claims(self):
        value = packet()
        value["sources"] += [
            {**value["sources"][0], "id": "S2", "context_complete": False},
            {**value["sources"][0], "id": "S3", "retrieved_at": "2020-01-01T00:00:00.000Z"},
        ]
        value["claims"] += [
            {"id": "C2", "text": "Incomplete", "source_ids": ["S2"]},
            {"id": "C3", "text": "Stale", "source_ids": ["S3"]},
            {"id": "C4", "text": "Missing quote", "source_ids": ["S1"], "quote": "does not exist"},
        ]
        value["claims"][0]["quote"] = "supports\n  staging deployments"
        result = self.run_packet(value)
        self.assertEqual(result["status"], "unresolved")
        self.assertEqual(result["coverage"]["expected"], 4)
        self.assertEqual(result["coverage"]["evaluated"], 1)
        self.assertEqual(
            [row["reason"] for row in result["claims"]][1:],
            ["incomplete_source", "stale_source", "quote_not_found"],
        )
        self.assertEqual(len(self.calls[0]["state"]["pairs"]), 1)

    def test_oversized_utf8_request_never_truncates_or_dispatches(self):
        self.setting(max_request_bytes=1024)
        value = packet()
        value["sources"][0]["excerpt"] = "界" * 3000
        value["sources"][0]["sha256"] = sha256(value["sources"][0]["excerpt"])
        value["claims"][0].pop("quote")
        result = self.run_packet(value, get_api_key=lambda: self.fail("key lookup"))
        self.assertEqual(result["reason"], "request_too_large")
        self.assertEqual(result["coverage"]["unresolved"], 1)
        self.assertEqual(self.calls, [])

    def test_missing_credentials_and_transport_failure_remain_nonjudgments(self):
        result = self.run_packet(get_api_key=lambda: None)
        self.assertEqual(result["reason"], "credential_missing")
        for reason, status in (
            ("http_429", "unavailable"),
            ("deadline_exceeded", "unavailable"),
            ("invalid_response", "invalid"),
            ("response_too_large", "invalid"),
        ):
            with self.subTest(reason=reason):

                def fail(*args, reason=reason, **kwargs):
                    raise JevError(reason)

                result = self.run_packet(request=fail, no_cache=True)
                self.assertEqual(result["status"], status)
                self.assertIsNone(result["claims"][0]["relation"])

        def bad_response(body, **kwargs):
            response = response_for(body)
            response["model"] = "wrong"
            return response

        result = self.run_packet(request=bad_response, no_cache=True)
        self.assertEqual(result["status"], "invalid")
        self.assertIsNone(result["claims"][0]["relation"])

    def test_shadow_withholding_and_no_persist(self):
        value = packet()
        self.setting(claim_mode="shadow")
        result = self.run_packet(value)
        self.assertTrue(result["judgments_withheld"])
        for key in ("claims", "probabilities", "confidence", "sources"):
            self.assertNotIn(key, result)
        hidden = json.loads((self.root / result["report"]).read_text())
        self.assertEqual(hidden["claims"][0]["relation"], "supports")
        self.assertEqual(result["report_hash"], digest(hidden))
        self.assertEqual(self.run_packet(value, no_persist=True)["reason"], "shadow_requires_local_record")
        shutil.rmtree(self.root / ".factory/local")
        self.setting(claim_mode="advisory")
        result = self.run_packet(value, no_persist=True)
        self.assertEqual(result["claims"][0]["relation"], "supports")
        self.assertNotIn("record", result)
        self.assertFalse((self.root / ".factory/local").exists())

    def test_cache_binds_complete_input_candidate_rubric_and_settings(self):
        value = packet()
        self.assertFalse(self.run_packet(value)["cache_hit"])
        self.assertTrue(self.run_packet(value, get_api_key=lambda: self.fail("cache read key"))["cache_hit"])
        self.assertEqual(len(self.calls), 1)
        value["sources"][0]["reference"] = "another source"
        self.assertFalse(self.run_packet(value)["cache_hit"])
        (self.root / "candidate.txt").write_text("changed candidate")
        self.assertFalse(self.run_packet(value)["cache_hit"])
        rubric = self.root / ".factory" / semantic.RUBRIC
        content = json.loads(rubric.read_text())
        content["instructions"] += " Preserve qualifiers."
        rubric.write_text(json.dumps(content))
        self.assertFalse(self.run_packet(value)["cache_hit"])
        self.setting(cache_ttl_seconds=12)
        self.assertFalse(self.run_packet(value)["cache_hit"])
        self.assertEqual(len(self.calls), 5)

    def test_corrupt_expired_and_invalid_cache_are_misses(self):
        value = packet()
        self.run_packet(value)
        cache_path = self.files("cache")[0]
        valid = json.loads(cache_path.read_text())
        for bad in (
            "{malformed",
            json.dumps({**valid, "created_at": "2020-01-01T00:00:00Z"}),
            json.dumps({**valid, "response": {**valid["response"], "model": "wrong"}}),
        ):
            cache_path.write_text(bad)
            self.assertFalse(self.run_packet(value)["cache_hit"])
        self.assertEqual(len(self.calls), 4)

    def test_no_cache_still_records_without_using_cached_advice(self):
        value = packet()
        self.run_packet(value)
        prior = self.files("cache")[0].read_bytes()
        result = self.run_packet(value, no_cache=True)
        self.assertFalse(result["cache_hit"])
        self.assertEqual(self.files("cache")[0].read_bytes(), prior)
        self.assertTrue((self.root / result["record"]).is_file())
        self.assertEqual(len(self.calls), 2)

    def test_rubric_or_schema_changed_during_preparation_prevents_dispatch(self):
        for relative in (semantic.RUBRIC, "schemas/semantic.schema.json"):
            with self.subTest(relative=relative):
                original = semantic.validate

                def validate_then_change(root, kind, value, original=original, relative=relative):
                    result = original(root, kind, value)
                    if kind == "semantic":
                        target = root / ".factory" / relative
                        contents = json.loads(target.read_text())
                        contents["description"] = "replacement after validation"
                        target.write_text(json.dumps(contents))
                    return result

                with patch.object(semantic, "validate", side_effect=validate_then_change):
                    result = self.run_packet(get_api_key=lambda: self.fail("key lookup"))
                self.assertEqual(result["status"], "invalid")
                self.assertIsNone(result["claims"][0]["relation"])

    def test_input_candidate_or_rubric_changes_during_request_discard_advice(self):
        for kind in ("candidate", "input", "rubric"):
            with self.subTest(kind=kind):
                value = packet()
                write_json(self.root, ".factory/local/claims.json", value)

                def mutate(body, kind=kind, value=value, **kwargs):
                    if kind == "candidate":
                        (self.root / "changing.txt").write_text(now())
                    elif kind == "input":
                        value["claims"][0]["text"] += " changed"
                        write_json(self.root, ".factory/local/claims.json", value)
                    else:
                        target = self.root / ".factory" / semantic.RUBRIC
                        target.write_text(target.read_text() + "\n")
                    return response_for(body)

                result = self.run_packet(
                    input_data=None, input_path=".factory/local/claims.json", request=mutate, no_cache=True
                )
                self.assertEqual(result["status"], "invalid")
                self.assertEqual(result["reason"], "inputs_changed")
                self.assertIsNone(result["claims"][0]["relation"])

    def test_disable_or_mode_change_mid_request_suppresses_all_publication(self):
        for change, expected in (
            ({"enabled": False}, "disabled_during_request"),
            ({"claim_mode": "shadow"}, "configuration_changed"),
        ):
            with self.subTest(change=change):
                self.setting(enabled=True, claim_mode="advisory")

                def mutate(body, change=change, **kwargs):
                    self.setting(**change)
                    return response_for(body)

                result = self.run_packet(request=mutate)
                self.assertEqual(result["status"], "skipped")
                self.assertEqual(result["reason"], expected)
                self.assertEqual(self.files("advisory") + self.files("shadow") + self.files("cache"), [])

    def test_transient_toggle_is_detected_even_if_original_config_restored(self):
        def mutate(body, **kwargs):
            self.setting(enabled=False)
            self.setting(enabled=True)
            return response_for(body)

        result = self.run_packet(request=mutate)
        self.assertEqual(result["reason"], "configuration_changed")
        self.assertEqual(result["status"], "skipped")

    def test_private_records_refuse_unignored_tracked_and_symlink_storage(self):
        (self.root / ".gitignore").write_text("")
        with self.assertRaises(FactoryError):
            self.run_packet()
        (self.root / ".gitignore").write_text(".factory/local/\n")
        (self.root / ".factory/local").mkdir(exist_ok=True)
        (self.root / ".factory/local/tracked").write_text("private")
        self.git("add", "-f", ".factory/local/tracked")
        with self.assertRaises(FactoryError):
            self.run_packet()
        self.git("rm", "--cached", "-f", ".factory/local/tracked")
        shutil.rmtree(self.root / ".factory/local")
        (self.root / ".factory/local").symlink_to(self.root / ".factory/schemas", target_is_directory=True)
        with self.assertRaisesRegex(FactoryError, "Symlink"):
            self.run_packet()
        self.assertEqual(self.calls, [])

    def test_cache_symlink_refused_without_request(self):
        value = packet()
        self.run_packet(value)
        cache = self.files("cache")[0]
        cache.unlink()
        cache.symlink_to(self.root / "factory.json")
        with self.assertRaisesRegex(FactoryError, "Symlink"):
            self.run_packet(value)
        self.assertEqual(len(self.calls), 1)

    def test_exclusive_lock_prevents_second_request(self):
        def first(body, **kwargs):
            result = self.run_packet()
            self.assertEqual(result["reason"], "local_request_busy")
            return response_for(body)

        result = self.run_packet(request=first)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(self.calls, [])
        self.assertFalse((self.root / semantic.LOCAL / "request.lock").exists())

    def test_input_refuses_directory_oversize_symlink_and_malformed_json(self):
        (self.root / ".factory/local").mkdir()
        target = self.root / ".factory/local/input.json"
        target.write_bytes(b"x" * 262145)
        for relative in (".factory/local", ".factory/local/input.json"):
            with self.subTest(relative=relative), self.assertRaises(FactoryError):
                self.run_packet(input_data=None, input_path=relative, no_persist=True)
        target.unlink()
        target.symlink_to(self.root / "factory.json")
        with self.assertRaisesRegex(FactoryError, "Symlink"):
            self.run_packet(input_data=None, input_path=".factory/local/input.json", no_persist=True)
        target.unlink()
        target.write_text("{SECRET malformed")
        with self.assertRaisesRegex(FactoryError, "^Invalid semantic JSON$"):
            self.run_packet(input_data=None, input_path=".factory/local/input.json", no_persist=True)
        self.assertEqual(self.calls, [])

    def test_cancellation_at_cache_or_report_write_rolls_back_new_artifacts(self):
        for category in ("cache", "advisory"):
            with self.subTest(category=category):
                cancel = threading.Event()
                original = semantic.write_bytes

                def cancel_after_write(
                    root, relative, data, *args, original=original, category=category, cancel=cancel, **kwargs
                ):
                    original(root, relative, data, *args, **kwargs)
                    if "/" + category + "/" in relative:
                        cancel.set()

                with patch.object(semantic, "write_bytes", side_effect=cancel_after_write):
                    result = self.run_packet(cancel=cancel)
                self.assertEqual(result["reason"], "canceled")
                self.assertEqual(result["status"], "skipped")
                self.assertEqual(self.files("cache") + self.files("advisory"), [])

    def test_canceled_cache_replacement_restores_exact_previous_bytes_and_reports(self):
        value = packet()
        self.run_packet(value)
        cache = self.files("cache")[0]
        old = b"{malformed previous cache bytes"
        cache.write_bytes(old)
        old_reports = {p: p.read_bytes() for p in self.files("advisory")}
        cancel, original = threading.Event(), semantic.write_bytes

        def cancel_after_report(root, relative, data, *args, **kwargs):
            original(root, relative, data, *args, **kwargs)
            if "/advisory/" in relative:
                cancel.set()

        with patch.object(semantic, "write_bytes", side_effect=cancel_after_report):
            result = self.run_packet(value, cancel=cancel)
        self.assertEqual(result["reason"], "canceled")
        self.assertEqual(cache.read_bytes(), old)
        self.assertEqual({p: p.read_bytes() for p in self.files("advisory")}, old_reports)

    def test_canceled_cache_hit_preserves_existing_cache_and_reports(self):
        value = packet()
        self.run_packet(value)
        old = {p: p.read_bytes() for p in self.files("cache") + self.files("advisory")}
        cancel, original = threading.Event(), semantic.write_bytes

        def cancel_after_report(root, relative, data, *args, **kwargs):
            original(root, relative, data, *args, **kwargs)
            if "/advisory/" in relative:
                cancel.set()

        with patch.object(semantic, "write_bytes", side_effect=cancel_after_report):
            result = self.run_packet(value, cancel=cancel)
        self.assertEqual(result["reason"], "canceled")
        self.assertEqual({p: p.read_bytes() for p in self.files("cache") + self.files("advisory")}, old)
        self.assertEqual(len(self.calls), 1)

    def test_post_write_error_rollback_and_cleanup_failure_are_local_errors(self):
        original = semantic.write_bytes

        def fail_after_report(root, relative, data, *args, **kwargs):
            original(root, relative, data, *args, **kwargs)
            if "/advisory/" in relative:
                raise FactoryError("simulated storage failure after commit")

        with (
            patch.object(semantic, "write_bytes", side_effect=fail_after_report),
            self.assertRaisesRegex(FactoryError, "storage failure"),
        ):
            self.run_packet()
        self.assertEqual(self.files("cache") + self.files("advisory"), [])
        with (
            patch.object(semantic, "write_bytes", side_effect=fail_after_report),
            patch.object(semantic, "_rollback_owned", side_effect=OSError("simulated removal failure")),
            self.assertRaisesRegex(FactoryError, "publication cleanup failed"),
        ):
            self.run_packet()

    def test_late_candidate_change_after_report_suppresses_and_removes_advice(self):
        original = semantic.write_bytes

        def change_after_report(root, relative, data, *args, **kwargs):
            original(root, relative, data, *args, **kwargs)
            if "/advisory/" in relative:
                (root / "candidate.txt").write_text("late candidate mutation")

        with patch.object(semantic, "write_bytes", side_effect=change_after_report):
            result = self.run_packet(no_cache=True)
        self.assertEqual(result["reason"], "inputs_changed")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(self.files("cache") + self.files("advisory"), [])

    def test_successor_cache_and_oversized_previous_cache_are_preserved(self):
        value = packet()
        self.run_packet(value)
        cache = self.files("cache")[0]
        oversized = b"x" * (65536 + 4097)
        cache.write_bytes(oversized)
        self.assertFalse(self.run_packet(value)["cache_hit"])
        self.assertEqual(cache.read_bytes(), oversized)
        cache.write_bytes(b"malformed previous")
        cancel, original = threading.Event(), semantic.write_bytes
        successor = b"successor must remain"

        def successor_after_report(root, relative, data, *args, **kwargs):
            original(root, relative, data, *args, **kwargs)
            if "/advisory/" in relative:
                cache.write_bytes(successor)
                cancel.set()

        with patch.object(semantic, "write_bytes", side_effect=successor_after_report):
            self.assertEqual(self.run_packet(value, cancel=cancel)["reason"], "canceled")
        self.assertEqual(cache.read_bytes(), successor)

    def test_mode_change_at_lock_release_rolls_back_all_owned_records(self):
        original = semantic._local_lock
        releases = [0]

        @contextmanager
        def mutate_after_release(root):
            with original(root) as acquired:
                yield acquired
            releases[0] += 1
            if releases[0] == 1:
                self.setting(claim_mode="shadow")

        with patch.object(semantic, "_local_lock", mutate_after_release):
            result = self.run_packet()
        self.assertEqual(result["reason"], "configuration_changed")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(self.files("cache") + self.files("advisory"), [])

    def test_cancel_during_observer_cleanup_is_checked_before_publication(self):
        cancel = threading.Event()
        original = semantic._Control.close

        def cancel_on_close(control):
            original(control)
            cancel.set()

        with patch.object(semantic._Control, "close", cancel_on_close):
            result = self.run_packet(cancel=cancel)
        self.assertEqual(result["reason"], "canceled")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(self.files("cache") + self.files("advisory"), [])

    def test_source_becoming_stale_during_request_invalidates_advice(self):
        self.setting(max_source_age_hours=1)
        value = packet()
        initial = semantic._time(value["sources"][0]["retrieved_at"]) / 1000
        with patch.object(semantic.time, "time", side_effect=[initial, initial + 3601]):
            result = self.run_packet(value, no_persist=True)
        self.assertEqual(result["reason"], "inputs_changed")
        self.assertEqual(result["status"], "invalid")
        self.assertIsNone(result["claims"][0]["relation"])

    def test_confidence_threshold_marks_accepted_or_needs_review(self):
        for threshold, confidence, expected in (
            (None, 0.95, "accepted"),
            (None, 0.8, "accepted"),
            (None, 0.6, "needs_review"),
            (0.5, 0.6, "accepted"),
            (0.99, 0.95, "needs_review"),
        ):
            with self.subTest(threshold=threshold, confidence=confidence):
                self.config["jev"].pop("claim_accept_confidence", None)
                if threshold is not None:
                    self.config["jev"]["claim_accept_confidence"] = threshold
                self.save_config()

                def request(body, confidence=confidence, **kwargs):
                    return response_for(body, confidence=confidence)

                result = self.run_packet(request=request, no_persist=True)
                applied = 0.8 if threshold is None else threshold
                row = result["claims"][0]
                # Advisory output keeps the model's relation and confidence visible.
                self.assertEqual(
                    (row["status"], row["relation"], row["confidence"], row["review"]),
                    ("evaluated", "supports", confidence, expected),
                )
                self.assertEqual(result["status"], "complete")
                self.assertEqual(
                    result["review"],
                    {
                        "claim_accept_confidence": applied,
                        "accepted": int(expected == "accepted"),
                        "needs_review": int(expected == "needs_review"),
                    },
                )
                self.assertEqual(result["provenance"]["claim_accept_confidence"], applied)
                self.assertNotIn("needs_review", result["coverage"])

    def test_invalid_threshold_is_rejected_clearly(self):
        for value in (1.5, -0.01, True, "0.8", float("nan"), None):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(FactoryError, "jev.claim_accept_confidence.*0 to 1"),
            ):
                semantic.semantic_settings({"jev": {"claim_accept_confidence": value}})
        for value in (0, 0.5, 1):
            self.assertEqual(
                semantic.semantic_settings({"jev": {"claim_accept_confidence": value}})[
                    "claim_accept_confidence"
                ],
                value,
            )
        self.setting(claim_accept_confidence=1.5)
        with self.assertRaises(FactoryError):
            self.run_packet(request=lambda *_, **__: self.fail("request"))

    def test_unavailable_rows_have_no_review_state(self):
        def fail(*args, **kwargs):
            raise JevError("http_429")

        result = self.run_packet(request=fail, no_cache=True)
        self.assertIsNone(result["claims"][0]["review"])
        self.assertEqual(result["review"]["accepted"] + result["review"]["needs_review"], 0)

    def test_shadow_withholds_review_state_and_counts(self):
        self.setting(claim_mode="shadow")
        results = {}
        for confidence in (0.95, 0.6):

            def request(body, confidence=confidence, **kwargs):
                return response_for(body, confidence=confidence)

            result = self.run_packet(request=request, no_cache=True)
            hidden = json.loads((self.root / result["report"]).read_text())
            self.assertEqual(
                hidden["claims"][0]["review"], "accepted" if confidence >= 0.8 else "needs_review"
            )
            self.assertEqual(result["report_hash"], digest(hidden))
            results[confidence] = result
        for result in results.values():
            text = json.dumps(result)
            for leaked in ("review", "accepted", "confidence", "supports", "claims"):
                self.assertNotIn(leaked, text)
        # Aside from the report path/hash and timing, output is independent of the judgment.
        strip = lambda value: {
            key: item for key, item in value.items() if key not in ("report", "report_hash", "elapsed_ms")
        }
        self.assertEqual(strip(results[0.95]), strip(results[0.6]))

    def test_threshold_change_never_reuses_stale_review_decision(self):
        value = packet()

        def request(body, **kwargs):
            self.calls.append(copy.deepcopy(body))
            return response_for(body, confidence=0.7)

        first = self.run_packet(value, request=request)
        self.assertEqual(first["claims"][0]["review"], "needs_review")
        cached = json.loads(self.files("cache")[0].read_text())
        self.assertNotIn("review", json.dumps(cached))
        hit = self.run_packet(value, request=request, get_api_key=lambda: self.fail("cache read key"))
        self.assertTrue(hit["cache_hit"])
        self.assertEqual(hit["claims"][0]["review"], "needs_review")
        self.setting(claim_accept_confidence=0.6)
        changed = self.run_packet(value, request=request)
        self.assertFalse(changed["cache_hit"])
        self.assertEqual(changed["claims"][0]["review"], "accepted")
        self.assertEqual(changed["provenance"]["claim_accept_confidence"], 0.6)
        self.assertNotEqual(changed["provenance"]["settings"], first["provenance"]["settings"])
        self.assertEqual(len(self.calls), 2)
        # A cache hit recomputes review from the raw answer with the current threshold.
        cache_file = self.files("cache")
        self.assertEqual(len(cache_file), 2)
        with patch.object(semantic, "_review", side_effect=semantic._review) as review:
            again = self.run_packet(value, request=request, get_api_key=lambda: self.fail("key"))
        self.assertTrue(again["cache_hit"])
        review.assert_called_once_with(0.7, 0.6)

    def test_status_reports_threshold_and_bounded_retries_without_network(self):
        with (
            patch.object(semantic, "request_jev", side_effect=AssertionError("network")),
            patch.object(semantic, "_context", side_effect=AssertionError("candidate read")),
        ):
            status = semantic.semantic_status(self.root)
            self.assertEqual(status["claim_accept_confidence"], 0.8)
            self.assertEqual(status["limits"]["requests_per_invocation"], 1)
            self.assertEqual(status["limits"]["automatic_retries"], 2)
            self.assertEqual(status["limits"]["retry_bound"], "bounded by deadline_ms")
            self.assertEqual((status["credential"], status["network"]), ("not_checked", "not_checked"))
            self.setting(claim_accept_confidence=0.5)
            self.assertEqual(semantic.semantic_status(self.root)["claim_accept_confidence"], 0.5)
        self.assertFalse((self.root / semantic.LOCAL).exists())

    def test_parser_alias_example_status_and_exit_codes(self):
        parser = argparse.ArgumentParser()
        parser.set_defaults(root=self.root)
        semantic.add_parser(parser.add_subparsers())
        args = parser.parse_args(["jev", "example"])
        result = args.handler(args)
        self.assertEqual(result["sources"][0]["sha256"], sha256(result["sources"][0]["excerpt"]))
        self.assertEqual(
            parser.parse_args(["semantic", "status"]).handler(parser.parse_args(["semantic", "status"]))[
                "network"
            ],
            "not_checked",
        )
        write_json(self.root, ".factory/local/input.json", packet())
        args = parser.parse_args(
            ["semantic", "check", "--input", ".factory/local/input.json", "--no-persist"]
        )
        with patch.dict(
            os.environ, {name: os.environ[name] for name in ("XDG_CONFIG_HOME", "APPDATA")}, clear=True
        ):
            result = args.handler(args)
        self.assertEqual(result["_exit_code"], 2)
        self.setting(enabled=False)
        result = args.handler(args)
        self.assertEqual(result["status"], "skipped")
        self.assertNotIn("_exit_code", result)


if __name__ == "__main__":
    unittest.main()
