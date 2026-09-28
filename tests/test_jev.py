"""Mock-only provider contract and bounded HTTPS transport checks."""

import copy
import io
import json
import threading
import time
import unittest
from unittest import mock

from software_factory import jev
from software_factory.jev import RELATIONS, JevError, request_jev, validate_jev_response


def response_for(body, choices=("supports",)):
    return {
        "model": body["model"],
        "answers": {
            key: {
                "type": "choice",
                "choice": choices[i % len(choices)],
                "confidence": 0.91,
                "probabilities": {
                    option: 0.8
                    if option == choices[i % len(choices)]
                    else 0.2 / (len(question["criteria"]) - 1)
                    for option in question["criteria"]
                },
            }
            for i, (key, question) in enumerate(body["questions"].items())
        },
        "usage": {"input_tokens": 123, "output_tokens": 0},
    }


class MockResponse:
    status = 200

    def __init__(self, data, *, status=200, length=None, delay=0, headers=None):
        self.stream = io.BytesIO(data)
        self.status, self.length, self.delay = status, length, delay
        self.headers = headers or {}

    def getheader(self, name):
        return self.length if name == "Content-Length" else self.headers.get(name)

    def read(self, size):
        if self.delay:
            time.sleep(self.delay)
        return self.stream.read(size)

    read1 = read


class JevTests(unittest.TestCase):
    def setUp(self):
        self.body = {
            "model": "jev-1.13.0",
            "state": {},
            "questions": {
                "q0": {
                    "type": "choice",
                    "instructions": "Assess the supplied claim.",
                    "criteria": {relation: relation for relation in RELATIONS},
                }
            },
        }

    def request(self, response=None, *, factory=None, **options):
        self.calls, self.closed = [], 0
        owner = self
        # A list supplies one response per successive attempt.
        responses = iter(response) if isinstance(response, list) else None

        class MockConnection:
            sock = None

            def __init__(self, host, timeout):
                self.host, self.timeout = host, timeout

            def request(self, method, target, body, headers):
                owner.calls.append(
                    {
                        "host": self.host,
                        "method": method,
                        "target": target,
                        "body": json.loads(body),
                        "headers": headers,
                    }
                )

            def getresponse(self):
                return next(responses) if responses is not None else response

            def close(self):
                owner.closed += 1

        return request_jev(
            self.body,
            api_key="mock-not-a-secret",
            deadline_ms=options.pop("deadline_ms", 3000),
            max_response_bytes=options.pop("max_response_bytes", 65536),
            connection_factory=factory or MockConnection,
            **options,
        )

    def test_valid_contract_and_ties(self):
        value = response_for(self.body)
        value["internal_data"] = "discard"
        self.assertNotIn("internal_data", validate_jev_response(value, self.body["model"], ["q0"]))
        value["answers"]["q0"]["probabilities"] = {key: 0.2 for key in RELATIONS}
        self.assertEqual(
            validate_jev_response(value, self.body["model"], ["q0"])["answers"]["q0"]["choice"],
            "supports",
        )

    def test_reject_incomplete_wrong_or_nonfinite_contract(self):
        def change(path, replacement):
            value = response_for(self.body)
            current = value
            for key in path[:-1]:
                current = current[key]
            current[path[-1]] = replacement
            return value

        cases = [
            change(["answers"], {}),
            change(["answers"], {"q0": {}, "extra": {}}),
            change(["model"], "jev-latest"),
            change(["answers", "q0", "type"], "number"),
            change(["answers", "q0", "choice"], "unknown"),
            change(["answers", "q0", "choice"], "mixed"),
            change(["answers", "q0", "confidence"], float("nan")),
            change(["answers", "q0", "confidence"], True),
            change(["answers", "q0", "confidence"], 10**1000),
            change(["answers", "q0", "probabilities", "supports"], float("inf")),
            change(["answers", "q0", "probabilities", "supports"], 0),
            change(["answers", "q0", "probabilities", "supports"], True),
            change(
                ["answers", "q0", "probabilities"],
                {key: 0.25 for key in RELATIONS[:-1]},
            ),
            change(["answers", "q0", "probabilities", "extra"], 0),
            change(["usage", "input_tokens"], -1),
            change(["usage", "output_tokens"], 0.5),
            change(["usage", "input_tokens"], True),
            change(["usage", "input_tokens"], 2**53),
            change(["usage"], None),
        ]
        for value in cases:
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(JevError, "invalid_response"),
            ):
                validate_jev_response(value, self.body["model"], ["q0"])

    def routing_body(self):
        return {
            "model": "jev-1.13.0",
            "state": {"task": "Implement the supplied specification"},
            "questions": {
                "implementer": {
                    "type": "choice",
                    "instructions": "Select the implementer from state candidates.",
                    "criteria": {
                        "candidate_a": "First eligible candidate",
                        "candidate_b": "Second eligible candidate",
                        "abstain": "Insufficient evidence",
                    },
                },
                "reviewer": {
                    "type": "choice",
                    "instructions": "Select the reviewer from state candidates.",
                    "criteria": {
                        "review_a": "Eligible reviewer",
                        "abstain": "Insufficient evidence",
                    },
                },
            },
        }

    def test_dynamic_choices_are_bound_to_each_question(self):
        body = self.routing_body()
        choices = {key: question["criteria"] for key, question in body["questions"].items()}
        result = validate_jev_response(
            response_for(body, ("candidate_b", "review_a")),
            body["model"],
            body["questions"],
            choices=choices,
        )
        self.assertEqual(result["answers"]["implementer"]["choice"], "candidate_b")
        self.assertEqual(set(result["answers"]["reviewer"]["probabilities"]), {"review_a", "abstain"})
        abstention = validate_jev_response(
            response_for(body, ("abstain",)),
            body["model"],
            body["questions"],
            choices=choices,
        )
        self.assertEqual(abstention["answers"]["implementer"]["choice"], "abstain")
        with self.assertRaisesRegex(JevError, "invalid_response"):
            validate_jev_response(
                response_for(body, ("candidate_b", "review_a")),
                body["model"],
                body["questions"],
            )

    def test_dynamic_choices_reject_swapped_missing_extra_or_invalid_answers(self):
        body = self.routing_body()
        valid = response_for(body, ("candidate_a", "review_a"))
        choices = {key: list(question["criteria"]) for key, question in body["questions"].items()}
        changed = []
        for transform in (
            lambda value: value["answers"].pop("reviewer"),
            lambda value: value["answers"].update(extra=value["answers"]["reviewer"]),
            lambda value: value["answers"]["reviewer"].update(choice="candidate_a"),
            lambda value: value["answers"]["reviewer"].update(choice="abstain"),
            lambda value: value["answers"]["reviewer"]["probabilities"].pop("abstain"),
            lambda value: value["answers"]["reviewer"]["probabilities"].update(candidate_a=0),
            lambda value: value["answers"]["reviewer"].update(type="score"),
            lambda value: value["answers"]["reviewer"].update(confidence=True),
            lambda value: value["answers"]["reviewer"]["probabilities"].update(review_a=float("nan")),
            lambda value: value["answers"]["reviewer"]["probabilities"].update(review_a=0.7),
            lambda value: value["usage"].update(input_tokens=-1),
            lambda value: value.update(model="jev-other"),
        ):
            value = copy.deepcopy(valid)
            transform(value)
            changed.append(value)
        for value in changed:
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(JevError, "invalid_response"),
            ):
                validate_jev_response(value, body["model"], body["questions"], choices=choices)

    def test_dynamic_choice_contract_is_bounded_and_unambiguous(self):
        body = self.routing_body()
        value = response_for(body, ("candidate_a", "review_a"))
        for choices in (
            {},
            {"implementer": ["candidate_a"]},
            {"implementer": [], "reviewer": ["review_a"]},
            {"implementer": "candidate_a", "reviewer": ["review_a"]},
            {"implementer": ["candidate_a", "candidate_a"], "reviewer": ["review_a"]},
            {
                "implementer": ["c" + str(i) for i in range(256)],
                "reviewer": ["review_a"],
            },
            {"implementer": ["candidate_a", None], "reviewer": ["review_a"]},
        ):
            with (
                self.subTest(choices=choices),
                self.assertRaisesRegex(JevError, "invalid_response"),
            ):
                validate_jev_response(value, body["model"], body["questions"], choices=choices)
        # Full provider option bound is accepted, including generator question IDs.
        options = ["c" + str(i) for i in range(255)]
        answer = {
            "model": body["model"],
            "answers": {
                "q": {
                    "type": "choice",
                    "choice": "c0",
                    "confidence": 1,
                    "probabilities": {key: 1 if key == "c0" else 0 for key in options},
                }
            },
            "usage": {"input_tokens": 1, "output_tokens": 0},
        }
        self.assertEqual(
            validate_jev_response(answer, body["model"], iter(["q"]), choices={"q": options})["answers"]["q"][
                "choice"
            ],
            "c0",
        )

    def test_transport_derives_allowed_choices_from_actual_request(self):
        self.body = self.routing_body()
        result = self.request(
            MockResponse(json.dumps(response_for(self.body, ("candidate_b", "review_a"))).encode())
        )
        self.assertEqual(result["answers"]["implementer"]["choice"], "candidate_b")
        self.assertEqual(len(self.calls), 1)
        with self.assertRaisesRegex(JevError, "invalid_response"):
            # A claim-support response cannot masquerade as a routing response.
            self.request(
                MockResponse(
                    json.dumps(
                        response_for(
                            {
                                "model": self.body["model"],
                                "questions": {
                                    key: {"criteria": dict.fromkeys(RELATIONS)}
                                    for key in self.body["questions"]
                                },
                            }
                        )
                    ).encode()
                )
            )

    def test_transport_rejects_invalid_choice_contract_before_connection(self):
        for question in (
            {"type": "score", "criteria": ["low", "high"]},
            {"type": "choice", "criteria": {}},
            {"type": "choice", "criteria": {"c" + str(i): None for i in range(256)}},
        ):
            self.body["questions"] = {"q0": question}
            with (
                self.subTest(question=question),
                self.assertRaisesRegex(JevError, "invalid_request"),
            ):
                self.request(
                    factory=lambda *_args, **_kwargs: self.fail("Invalid request opened a connection")
                )

    def test_transport_contract_is_frozen_to_transmitted_bytes(self):
        self.body = self.routing_body()
        caller_body = self.body

        class MutatingConnection:
            sock = None

            def __init__(self, *_args, **_kwargs):
                pass

            def request(self, method, target, body, headers):
                self.sent = json.loads(body)
                caller_body["model"] = "changed-after-send"
                caller_body["questions"]["reviewer"]["criteria"]["injected"] = "Not sent"

            def getresponse(self):
                return MockResponse(json.dumps(response_for(self.sent, ("candidate_a", "review_a"))).encode())

            def close(self):
                pass

        result = self.request(factory=MutatingConnection)
        self.assertEqual(result["model"], "jev-1.13.0")
        self.assertNotIn("injected", result["answers"]["reviewer"]["probabilities"])

    def test_one_fixed_https_request_and_expected_auth(self):
        result = self.request(MockResponse(json.dumps(response_for(self.body)).encode()))
        self.assertEqual(result["usage"]["input_tokens"], 123)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["host"], "api.typesafe.ai")
        self.assertEqual(self.calls[0]["target"], "/v1/systemone")
        self.assertEqual(self.calls[0]["method"], "POST")
        self.assertEqual(self.calls[0]["headers"]["Authorization"], "Bearer mock-not-a-secret")
        self.assertGreaterEqual(self.closed, 1)

    def test_no_redirects_or_error_body_disclosure_and_bounded_retries(self):
        # Intentional change: 408, 429 and 5xx (incl. 529) are now retried at
        # most twice per official TypeSafe guidance; other statuses never are.
        pauses = []
        with mock.patch.object(jev, "_pause", lambda seconds, _cancel: pauses.append(seconds)):
            for status in (301, 302, 307, 400, 401, 403, 404, 422, 408, 429, 500, 503, 529):
                with (
                    self.subTest(status=status),
                    self.assertRaisesRegex(JevError, "^http_" + str(status) + "$") as raised,
                ):
                    self.request(MockResponse(b"SECRET PRIVATE BODY", status=status))
                expected = 3 if status in (408, 429, 500, 503, 529) else 1
                self.assertEqual(len(self.calls), expected)
                self.assertEqual(raised.exception.attempts, expected)
                self.assertNotIn("SECRET", str(raised.exception))
        self.assertEqual(len(pauses), 2 * 5)

    def test_sanitize_transport_exception(self):
        calls = []

        def broken(*args, **kwargs):
            calls.append(1)
            raise RuntimeError("SECRET key and request")

        with (
            mock.patch.object(jev, "_pause", lambda *_: None),
            self.assertRaisesRegex(JevError, "^network_error$") as raised,
        ):
            self.request(factory=broken)
        # Connection errors are retried within the deadline, then reported sanitized.
        self.assertEqual(len(calls), 3)
        self.assertEqual(raised.exception.attempts, 3)

    def ok(self):
        return MockResponse(json.dumps(response_for(self.body)).encode())

    def test_retry_429_then_success_reports_attempts(self):
        attempts, pauses = [], []
        with (
            mock.patch.object(jev, "_pause", lambda seconds, _cancel: pauses.append(seconds)),
            mock.patch.object(jev.random, "random", lambda: 0.0),
        ):
            result = self.request([MockResponse(b"busy", status=429), self.ok()], on_attempt=attempts.append)
        self.assertEqual(result["usage"]["input_tokens"], 123)
        self.assertEqual((len(self.calls), attempts, pauses), (2, [1, 2], [0.5]))

    def test_retry_529_twice_uses_exponential_backoff_with_jitter(self):
        attempts, pauses = [], []
        with (
            mock.patch.object(jev, "_pause", lambda seconds, _cancel: pauses.append(seconds)),
            mock.patch.object(jev.random, "random", lambda: 1.0),
        ):
            result = self.request(
                [MockResponse(b"", status=529), MockResponse(b"", status=529), self.ok()],
                on_attempt=attempts.append,
            )
        self.assertEqual(result["answers"]["q0"]["choice"], "supports")
        self.assertEqual(attempts, [1, 2, 3])
        # Jitter shortens by at most 25%: 0.5 s and 1.0 s become 0.375 s and 0.75 s.
        self.assertEqual(pauses, [0.375, 0.75])
        self.assertLessEqual(jev._backoff(10), jev.BACKOFF_MAX_SECONDS)

    def test_three_server_errors_fail_after_three_attempts(self):
        with (
            mock.patch.object(jev, "_pause", lambda *_: None),
            self.assertRaisesRegex(JevError, "^http_500$") as raised,
        ):
            self.request([MockResponse(b"", status=500) for _ in range(4)])
        self.assertEqual((len(self.calls), raised.exception.attempts), (3, 3))

    def test_unauthorized_and_invalid_responses_are_not_retried(self):
        for response, code in (
            (MockResponse(b"", status=401), "http_401"),
            (MockResponse(b"{SECRET"), "invalid_response"),
        ):
            with (
                self.subTest(code=code),
                mock.patch.object(jev, "_pause", lambda *_: self.fail("Retried a permanent error")),
                self.assertRaisesRegex(JevError, "^" + code + "$") as raised,
            ):
                self.request([response, self.ok()])
            self.assertEqual((len(self.calls), raised.exception.attempts), (1, 1))

    def test_retries_never_exceed_the_deadline(self):
        # The backoff (>= 0.375 s) plus a minimal attempt window cannot fit in 300 ms.
        started = time.monotonic()
        with self.assertRaisesRegex(JevError, "^http_429$") as raised:
            self.request([MockResponse(b"", status=429), self.ok()], deadline_ms=300)
        self.assertEqual((len(self.calls), raised.exception.attempts), (1, 1))
        self.assertLess(time.monotonic() - started, 0.25)
        # A stalled attempt that consumes the deadline is not retried.
        with self.assertRaisesRegex(JevError, "^deadline_exceeded$") as raised:
            self.request([MockResponse(b"{}", delay=0.3), self.ok()], deadline_ms=40)
        self.assertEqual((len(self.calls), raised.exception.attempts), (1, 1))

    def test_numeric_retry_after_is_honored_only_when_it_fits(self):
        pauses = []
        with mock.patch.object(jev, "_pause", lambda seconds, _cancel: pauses.append(seconds)):
            self.request([MockResponse(b"", status=429, headers={"Retry-After": "1.5"}), self.ok()])
            self.assertEqual((pauses, len(self.calls)), ([1.5], 2))
            with self.assertRaisesRegex(JevError, "^http_503$"):
                self.request([MockResponse(b"", status=503, headers={"Retry-After": "60"}), self.ok()])
            self.assertEqual((pauses, len(self.calls)), ([1.5], 1))
        for value, expected in (("2", 2.0), ("0", 0.0), ("-1", None), ("nan", None), ("Wed, 21 Oct", None)):
            self.assertEqual(jev._retry_after(value), expected)
        started = time.monotonic()
        self.request([MockResponse(b"", status=429, headers={"Retry-After": "0.2"}), self.ok()])
        self.assertGreaterEqual(time.monotonic() - started, 0.2)

    def test_cancellation_interrupts_backoff(self):
        canceled = threading.Event()
        timer = threading.Timer(0.05, canceled.set)
        timer.start()
        started = time.monotonic()
        with (
            mock.patch.object(jev, "BACKOFF_INITIAL_SECONDS", 2.0),
            self.assertRaisesRegex(JevError, "^canceled$") as raised,
        ):
            self.request([MockResponse(b"", status=529), self.ok()], cancel=canceled)
        timer.join()
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual((len(self.calls), raised.exception.attempts), (1, 1))

    def test_max_retries_zero_restores_single_request(self):
        with self.assertRaisesRegex(JevError, "^http_429$"):
            self.request([MockResponse(b"", status=429), self.ok()], max_retries=0)
        self.assertEqual(len(self.calls), 1)

    def test_reject_malformed_nonfinite_duplicate_utf8_and_oversized_response(self):
        for raw, expected in (
            (b"{SECRET", "invalid_response"),
            (b"\xff", "invalid_response"),
            (b'{"model":NaN}', "invalid_response"),
            (b'{"model":"a","model":"b"}', "invalid_response"),
            (b"x" * 1025, "response_too_large"),
        ):
            with self.subTest(raw=raw[:20]), self.assertRaisesRegex(JevError, expected):
                self.request(MockResponse(raw), max_response_bytes=1024)
        with self.assertRaisesRegex(JevError, "response_too_large"):
            self.request(MockResponse(b"", length="2048"), max_response_bytes=1024)

    def test_deadline_covers_stalled_headers_and_body(self):
        started = time.monotonic()
        with self.assertRaisesRegex(JevError, "deadline_exceeded"):
            self.request(MockResponse(b"{}", delay=0.3), deadline_ms=40)
        self.assertLess(time.monotonic() - started, 0.25)

        class SlowConnection:
            sock = None

            def __init__(self, *args, **kwargs):
                pass

            def request(self, *args, **kwargs):
                pass

            def getresponse(self):
                time.sleep(0.3)
                return MockResponse(b"{}")

            def close(self):
                pass

        started = time.monotonic()
        with self.assertRaisesRegex(JevError, "deadline_exceeded"):
            self.request(factory=SlowConnection, deadline_ms=40)
        self.assertLess(time.monotonic() - started, 0.25)

    def test_cancellation_and_precanceled_request(self):
        canceled = threading.Event()
        timer = threading.Timer(0.03, canceled.set)
        timer.start()
        started = time.monotonic()
        with self.assertRaisesRegex(JevError, "canceled"):
            self.request(MockResponse(b"{}", delay=0.3), cancel=canceled)
        timer.join()
        self.assertLess(time.monotonic() - started, 0.25)
        with self.assertRaisesRegex(JevError, "canceled"):
            self.request(MockResponse(b"{}"), cancel=canceled)
        self.assertEqual(self.calls, [])

    def test_truncated_body_is_invalid_even_when_json_is_complete(self):
        raw = json.dumps(response_for(self.body)).encode()
        with self.assertRaisesRegex(JevError, "invalid_response"):
            self.request(MockResponse(raw, length=str(len(raw) + 1)))

    def test_bounded_header_length(self):
        for length in ("-1", "private", "2.5"):
            with (
                self.subTest(length=length),
                self.assertRaisesRegex(JevError, "invalid_response"),
            ):
                self.request(MockResponse(b"{}", length=length))


if __name__ == "__main__":
    unittest.main()
