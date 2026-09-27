"""Bounded transport for TypeSafe's versioned Jev API.

The transport accepts only the fixed HTTPS endpoint. Errors expose stable codes,
never upstream bodies, supplied excerpts, keys, or exception messages. Transient
failures get a small number of backoff retries inside the caller's one deadline,
following TypeSafe's published guidance and official SDK retry defaults.
"""

from __future__ import annotations

import http.client
import json
import math
import queue
import random
import threading
import time
from collections.abc import Callable, Mapping

RELATIONS = (
    "supports",
    "contradicts",
    "not_addressed",
    "mixed",
    "insufficient_context",
)
MAX_CHOICES = 255
# Official SDK defaults: two retries on 408, 429, 5xx (including 529) and on
# connection errors/timeouts; backoff starts at 0.5 s, doubles, caps at 5 s and
# jitter shortens each wait by up to 25%. Retries never extend the deadline.
MAX_RETRIES = 2
RETRY_STATUSES = frozenset({408, 429, *range(500, 600)})
BACKOFF_INITIAL_SECONDS = 0.5
BACKOFF_MAX_SECONDS = 5.0
BACKOFF_JITTER = 0.25
# A retry is attempted only if at least this much deadline remains after waiting.
MIN_ATTEMPT_SECONDS = 0.25


class JevError(Exception):
    def __init__(self, code: str, *, retry_after: float | None = None):
        super().__init__(code)
        self.code = code
        self.retry_after = retry_after
        # Provider attempts made by request_jev before this error (0 if none).
        self.attempts = 0


def _retryable(error: JevError) -> bool:
    if error.code == "network_error":
        return True
    if error.code.startswith("http_"):
        try:
            return int(error.code[5:]) in RETRY_STATUSES
        except ValueError:
            return False
    return False


def _retry_after(value) -> float | None:
    """Numeric Retry-After seconds only; HTTP dates and invalid values are ignored."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if math.isfinite(seconds) and seconds >= 0 else None


def _backoff(retry: int) -> float:
    delay = min(BACKOFF_MAX_SECONDS, BACKOFF_INITIAL_SECONDS * 2 ** (retry - 1))
    return delay * (1 - BACKOFF_JITTER * random.random())


def _pause(seconds: float, cancel: threading.Event | None) -> None:
    """Wait between attempts; cancellation interrupts the wait immediately."""
    if cancel is None:
        time.sleep(seconds)
    elif cancel.wait(seconds):
        raise JevError("canceled")


def _probability(value):
    return type(value) in (int, float) and 0 <= value <= 1 and math.isfinite(value)


def _choice_contract(question_ids, choices=None):
    """Freeze the exact allowed answer keys, including question-specific options."""
    try:
        ids = tuple(question_ids)
        if not ids or any(not isinstance(key, str) or not key for key in ids) or len(set(ids)) != len(ids):
            raise ValueError()
        if choices is not None and (not isinstance(choices, Mapping) or set(choices) != set(ids)):
            raise ValueError()
        result = {}
        for question in ids:
            options = RELATIONS if choices is None else choices[question]
            if isinstance(options, (str, bytes)):
                raise TypeError()
            options = tuple(options)
            if (
                not 1 <= len(options) <= MAX_CHOICES
                or any(not isinstance(key, str) or not key for key in options)
                or len(set(options)) != len(options)
            ):
                raise ValueError()
            result[question] = options
        return result
    except (TypeError, ValueError, KeyError):
        raise JevError("invalid_response") from None


def validate_jev_response(value: dict, model: str, question_ids, *, choices=None) -> dict:
    """Validate exact per-question Choice options; default to claim relationships."""
    expected = _choice_contract(question_ids, choices)

    def bad():
        raise JevError("invalid_response")

    if (
        not isinstance(value, dict)
        or value.get("model") != model
        or not isinstance(value.get("answers"), dict)
        or set(value["answers"]) != set(expected)
        or not isinstance(value.get("usage"), dict)
    ):
        bad()
    for key in ("input_tokens", "output_tokens"):
        number = value["usage"].get(key)
        if type(number) is not int or not 0 <= number <= 9007199254740991:
            bad()
    answers = {}
    for question, options in expected.items():
        answer = value["answers"][question]
        if (
            not isinstance(answer, dict)
            or answer.get("type") != "choice"
            or answer.get("choice") not in options
            or not isinstance(answer.get("probabilities"), dict)
            or set(answer["probabilities"]) != set(options)
            or not _probability(answer.get("confidence"))
        ):
            bad()
        probabilities = answer["probabilities"]
        values = list(probabilities.values())
        if (
            not all(_probability(number) for number in values)
            or abs(sum(values) - 1) > 0.001
            or probabilities[answer["choice"]] + 0.001 < max(values)
        ):
            bad()
        answers[question] = {
            "type": "choice",
            "choice": answer["choice"],
            "probabilities": {key: probabilities[key] for key in options},
            "confidence": answer["confidence"],
        }
    return {
        "model": model,
        "answers": answers,
        "usage": {key: value["usage"][key] for key in ("input_tokens", "output_tokens")},
    }


def _reject_constant(_value):
    raise ValueError("Non-finite JSON number")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def strict_json(data: bytes):
    return json.loads(
        data.decode("utf-8"),
        parse_constant=_reject_constant,
        object_pairs_hook=_unique_object,
    )


def request_jev(
    body: dict,
    *,
    api_key: str,
    deadline_ms: int,
    max_response_bytes: int,
    cancel: threading.Event | None = None,
    connection_factory: Callable | None = None,
    max_retries: int = MAX_RETRIES,
    on_attempt: Callable[[int], None] | None = None,
) -> dict:
    """Bounded HTTP requests sharing one deadline covering DNS, TLS, headers and body.

    Retries at most ``max_retries`` times on HTTP 408, 429, 500-599 and on
    connection errors, with jittered exponential backoff or a numeric
    Retry-After. A retry happens only if its wait plus MIN_ATTEMPT_SECONDS fits
    before the deadline; otherwise the last error is raised. ``on_attempt(n)``
    is called before each provider attempt n (1-based); a raised JevError
    carries the same count in ``attempts``.

    A daemon worker lets the caller enforce the total deadline even if OS DNS or
    a peer stalls. Cancellation closes its socket, guards subsequent writes and
    interrupts a backoff wait. No worker result can be published after
    cancellation. DNS itself cannot be interrupted portably; its worker exits
    when the system call returns.
    """
    deadline = time.monotonic() + deadline_ms / 1000
    attempts = 0
    retries = max(0, int(max_retries))
    while True:
        try:
            if cancel is not None and cancel.is_set():
                raise JevError("canceled")
            if time.monotonic() >= deadline:
                raise JevError("deadline_exceeded")
            attempts += 1
            if on_attempt is not None:
                on_attempt(attempts)
            return _attempt(
                body,
                api_key=api_key,
                deadline=deadline,
                max_response_bytes=max_response_bytes,
                cancel=cancel,
                connection_factory=connection_factory,
            )
        except JevError as exc:
            exc.attempts = attempts
            if attempts > retries or not _retryable(exc):
                raise
            delay = exc.retry_after if exc.retry_after is not None else _backoff(attempts)
            if time.monotonic() + delay + MIN_ATTEMPT_SECONDS >= deadline:
                raise
            try:
                _pause(delay, cancel)
            except JevError as canceled:
                canceled.attempts = attempts
                raise canceled from None


def _attempt(body, *, api_key, deadline, max_response_bytes, cancel, connection_factory) -> dict:
    """One HTTP request bounded by the shared absolute monotonic deadline."""
    stopped = threading.Event()
    outcome = queue.Queue(maxsize=1)
    connection = [None]

    def check():
        if cancel is not None and cancel.is_set():
            raise JevError("canceled")
        if stopped.is_set() or time.monotonic() >= deadline:
            raise JevError("deadline_exceeded")

    class DeadlineHTTPSConnection(http.client.HTTPSConnection):
        def connect(self):
            check()
            super().connect()
            try:
                check()
            except BaseException:
                self.close()
                raise

        def send(self, data):
            check()
            super().send(data)
            check()

    def worker():
        conn = None
        try:
            check()
            # Validate against a snapshot of the bytes actually sent. Caller
            # mutation during an in-flight request cannot change allowed options.
            try:
                payload = json.dumps(
                    body, ensure_ascii=False, separators=(",", ":"), allow_nan=False
                ).encode()
                sent = strict_json(payload)
                questions = sent["questions"]
                if not isinstance(questions, dict) or not isinstance(sent["model"], str):
                    raise TypeError()
                if any(
                    not isinstance(question, dict)
                    or question.get("type") != "choice"
                    or not isinstance(question.get("criteria"), dict)
                    for question in questions.values()
                ):
                    raise ValueError()
                expected = _choice_contract(
                    questions,
                    {key: question["criteria"] for key, question in questions.items()},
                )
            except (KeyError, TypeError, ValueError, JevError):
                raise JevError("invalid_request") from None
            check()
            factory = connection_factory or DeadlineHTTPSConnection
            conn = factory("api.typesafe.ai", timeout=max(0.001, deadline - time.monotonic()))
            connection[0] = conn
            check()
            conn.request(
                "POST",
                "/v1/systemone",
                body=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": "Bearer " + api_key,
                },
            )
            check()
            response = conn.getresponse()
            check()
            if not 200 <= response.status < 300:
                raise JevError(
                    "http_" + str(response.status),
                    retry_after=_retry_after(response.getheader("Retry-After")),
                )
            announced = response.getheader("Content-Length")
            if announced is not None:
                try:
                    if int(announced) > max_response_bytes:
                        raise JevError("response_too_large")
                    if int(announced) < 0:
                        raise ValueError()
                except ValueError:
                    raise JevError("invalid_response") from None
            chunks, size = [], 0
            while True:
                check()
                sock = getattr(conn, "sock", None)
                if sock is not None:
                    sock.settimeout(max(0.001, deadline - time.monotonic()))
                # read1 returns available bytes so a slow trickle cannot reset a
                # per-read timer and evade the outer total deadline.
                reader = getattr(response, "read1", response.read)
                part = reader(min(16384, max_response_bytes + 1 - size))
                check()
                if not part:
                    break
                size += len(part)
                if size > max_response_bytes:
                    raise JevError("response_too_large")
                chunks.append(part)
            if announced is not None and size != int(announced):
                raise JevError("invalid_response")
            try:
                parsed = strict_json(b"".join(chunks))
            except (ValueError, UnicodeError):
                raise JevError("invalid_response") from None
            result = validate_jev_response(parsed, sent["model"], expected, choices=expected)
            check()
            outcome.put((True, result))
        except JevError as exc:
            outcome.put((False, exc))
        except (TimeoutError, OSError):
            outcome.put(
                (
                    False,
                    JevError("deadline_exceeded" if time.monotonic() >= deadline else "network_error"),
                )
            )
        except Exception:  # noqa: BLE001 - report sanitized transport failures
            outcome.put((False, JevError("network_error")))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass

    check()
    thread = threading.Thread(target=worker, name="factory-jev-request", daemon=True)
    thread.start()
    try:
        while True:
            check()
            try:
                success, value = outcome.get(timeout=min(0.02, max(0.001, deadline - time.monotonic())))
            except queue.Empty:
                continue
            check()
            if success:
                return value
            raise value
    finally:
        stopped.set()
        conn = connection[0]
        if conn is not None:
            # Shutdown wakes a blocked socket read without waiting for the worker.
            sock = getattr(conn, "sock", None)
            if sock is not None:
                try:
                    sock.shutdown(2)
                except OSError:
                    pass
            try:
                conn.close()
            except OSError:
                pass
