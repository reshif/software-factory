"""Optional, private, advisory-only Jev triage of failed check evidence.

One Choice question per failing check over fixed categories plus ``abstain``.
Following TypeSafe's confidence-routing pattern, a choice below
``jev.triage_accept_confidence`` (default 0.6) is left unclassified for a human.
Triage never changes mission state, tasks, attempts, repair budgets, evidence,
results or gates; it only writes a private report under .factory/local/.
"""

from __future__ import annotations

import json
import math
import os
import signal
import stat
import threading
import time
import uuid
from pathlib import Path

from . import auth
from .core import (
    FactoryError,
    assert_id,
    digest,
    git,
    load_config,
    now,
    private_dir,
    read_json,
    safe_path,
    sha256,
    validate,
    write_bytes,
)
from .jev import JevError, request_jev, validate_jev_response
from .redaction import bound_text, redact, redact_argv

LOCAL = ".factory/local/semantic/triage"
MAX_CHECKS = 8
TAIL_BYTES = 6144
# Raw tail read before masking; it is masked whole, then cut to TAIL_BYTES on a line.
READ_BYTES = 4 * TAIL_BYTES
MAX_LOG_BYTES = 64 * 1048576
# TypeSafe confidence-routing guidance: apply a routed choice only at or above
# the floor (examples use 0.5-0.6); lower-confidence choices go to a human.
DEFAULT_THRESHOLD = 0.6
CATEGORIES = {
    "flaky": "The failure is nondeterministic: timing, ordering, races or intermittent external "
    "dependencies; the same candidate would plausibly pass on rerun.",
    "environment": "The failure comes from the execution environment: missing tools, interpreter or "
    "dependency versions, permissions, network, disk or operating-system differences.",
    "test_defect": "The product behaves as intended but the test or its fixtures, expectations or "
    "assertions are wrong or outdated.",
    "product_defect": "The product code under test behaves incorrectly and the check correctly reports it.",
    "configuration": "The check definition or project configuration is wrong: argv, working directory, "
    "timeout, output limit, setup step or required settings.",
    "abstain": "The supplied argv, status and log tail are insufficient to choose one category.",
}
INSTRUCTIONS = (
    "Classify the most likely cause of one failed software check for a human engineer's diagnosis. "
    "Secrets in the log tail were masked and must be ignored. The tail may omit earlier output. "
)


def _threshold(settings):
    value = settings.get("triage_accept_confidence", DEFAULT_THRESHOLD)
    # Enforced here too, independent of the configuration schema version.
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 <= value <= 1
    ):
        raise FactoryError("Invalid jev.triage_accept_confidence: expected a number from 0 to 1")
    return value


def triage_settings(config):
    from .semantic import semantic_settings

    settings = semantic_settings(config)
    settings["triage_accept_confidence"] = _threshold(settings)
    return settings


def _private(root):
    private_dir(root)
    safe_path(root, LOCAL)
    try:
        git(root, "check-ignore", "--quiet", "--no-index", "--", LOCAL + "/")
        if git(root, "ls-files", "-z", "--", LOCAL):
            raise FactoryError("Tracked triage records")
    except FactoryError as exc:
        raise FactoryError(
            f"Private records require an effectively ignored, untracked {LOCAL}/ directory"
        ) from exc


def _refuse(message):
    return FactoryError(message, exit_code=2)


def _read_log(root, log, expected_path):
    """Hash the whole regular log file and return its bounded raw tail."""
    if log["path"] != expected_path:
        raise _refuse(f"Log path does not match evidence: {expected_path}")
    target = safe_path(root, expected_path)
    try:
        fd = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError as exc:
        raise _refuse(f"Cannot read evidence log {expected_path}") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_LOG_BYTES:
            raise _refuse(f"Evidence log is not a bounded regular file: {expected_path}")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(MAX_LOG_BYTES + 1)
    finally:
        os.close(fd)
    if len(data) > MAX_LOG_BYTES or sha256(data) != log["sha256"]:
        raise _refuse(f"Evidence log hash does not match recorded evidence: {expected_path}")
    return data[-READ_BYTES:], len(data)


def _trim(tail, size, budget):
    if len(tail) > budget:
        tail = tail[-budget:]
    if len(tail) < size:
        # Drop a partial first line so a cut cannot split a secret past redaction.
        newline = tail.find(b"\n")
        tail = tail[newline + 1 :] if newline != -1 else b""
    return tail


def _excerpts(root, mission_id, revision, item):
    assert_id(item["id"])
    base = f".factory/local/runs/{mission_id}/{revision}/{item['id']}"
    tails, sizes, hashes = {}, {}, {}
    for channel in ("stdout", "stderr"):
        log = item[f"{channel}_log"]
        tails[channel], sizes[channel] = _read_log(root, log, f"{base}.{channel}.log")
        hashes[channel] = log["sha256"]
    # Mask whole lines of the generous raw tail first, then cut the masked text on a line,
    # so a cut never splits a secret into an unmasked fragment.
    masked, masks = {}, 0
    for channel in ("stdout", "stderr"):
        raw = _trim(tails[channel], sizes[channel], READ_BYTES)
        masked[channel], count = redact(raw.decode("utf-8", errors="replace"))
        masks += count
    half = TAIL_BYTES // 2
    budgets = {"stdout": half, "stderr": half}
    for channel, other in (("stdout", "stderr"), ("stderr", "stdout")):
        used = len(masked[other].encode())
        if used < half:
            budgets[channel] = TAIL_BYTES - used
    texts = {channel: bound_text(masked[channel], budgets[channel], tail=True)[0] for channel in masked}
    return texts, masks, hashes


def _load_evidence(root, mission_id, revision):
    from .workflow import load_mission

    mission = load_mission(root, mission_id)
    reference = f".factory/missions/{mission_id}/evidence/{revision}/checks.json"
    if reference not in mission["evidence"]:
        raise _refuse(f"Evidence {revision} is not registered in mission {mission_id}")
    evidence = validate(root, "evidence", read_json(root, reference))
    if (
        evidence["mission_id"] != mission_id
        or evidence["id"] != revision
        or evidence["sequence"] != mission["evidence"].index(reference) + 1
    ):
        raise _refuse("Evidence registration disagrees with its recorded identity or sequence")
    return reference, evidence


def _unavailable(reason):
    return {
        "schema_version": 1,
        "operation": "check_triage",
        "status": "unavailable",
        "reason": reason,
        "advisory_only": True,
        "_exit_code": 2,
    }


def _mark(rows, status, reason):
    for row in rows:
        row.update(status=status, reason=reason, choice=None, probabilities=None, confidence=None)
        row.update(category=None, review=None)


def triage_checks(
    root,
    mission,
    revision,
    *,
    check=None,
    no_network=False,
    cancel=None,
    get_api_key=None,
    request=None,
):
    """Classify failed checks of registered evidence. Advisory only; never a gate."""
    from .checks import successful_check

    root = Path(root)
    assert_id(mission)
    assert_id(revision)
    settings = triage_settings(load_config(root))
    threshold = settings["triage_accept_confidence"]
    # Strict bypass paths precede evidence, logs, keys and storage.
    if not settings["enabled"]:
        return _unavailable("disabled")
    if no_network:
        return _unavailable("network_disallowed")
    if cancel is not None and cancel.is_set():
        return _unavailable("canceled")
    started = time.monotonic()
    reference, evidence = _load_evidence(root, mission, revision)
    evidence_hash = sha256(safe_path(root, reference).read_bytes())
    if check is not None and not any(item["id"] == check for item in evidence["checks"]):
        raise _refuse(f"Check {check} is not in evidence {revision}")
    failing = [
        item
        for item in evidence["checks"]
        if not successful_check(item) and (check is None or item["id"] == check)
    ]
    selected, omitted = failing[:MAX_CHECKS], [item["id"] for item in failing[MAX_CHECKS:]]
    _private(root)
    rows, state = [], []
    for item in selected:
        texts, masks, hashes = _excerpts(root, mission, revision, item)
        argv, count = redact_argv(item["command"])
        masks += count
        state.append(
            {
                "check": item["id"],
                "argv": argv,
                "status": item["status"],
                "exit_code": item["exit_code"],
                "signal": item["signal"],
                "duration_ms": item["duration_ms"],
                "output_truncated": item["truncated"],
                "log_tail": texts,
            }
        )
        rows.append(
            {
                "check": item["id"],
                "status": "pending",
                "reason": None,
                "log_sha256": hashes,
                "excerpt_sha256": digest(state[-1]),
                "masked": masks,
                "choice": None,
                "probabilities": None,
                "confidence": None,
                "category": None,
                "review": None,
            }
        )
    options = tuple(CATEGORIES)
    questions = {
        "q" + str(index): {
            "type": "choice",
            "instructions": INSTRUCTIONS
            + f"Use ONLY state.checks[{index}]: its argv, status, exit code, duration and log tail. "
            + "Choose abstain when the evidence does not support one category. The check ID is "
            + json.dumps(row["check"])
            + ".",
            "criteria": CATEGORIES,
        }
        for index, row in enumerate(rows)
    }
    body = {"model": settings["model"], "state": {"checks": state}, "questions": questions}
    report = {
        "schema_version": 1,
        "operation": "check_triage",
        "mode": settings["claim_mode"],
        "advisory_only": True,
        "mission_id": mission,
        "revision": revision,
        "evidence": reference,
        "evidence_sha256": evidence_hash,
        "triage_accept_confidence": threshold,
        "categories": list(options),
        "requested_model": settings["model"],
        "returned_model": None,
        "request_sha256": digest(body),
        "status": "complete",
        "reason": None,
        "created_at": now(),
        "usage": None,
        "attempts": 0,
        "elapsed_ms": 0,
        "omitted_checks": omitted,
        "checks": rows,
    }
    response = None
    if not rows:
        report.update(status="nothing_to_triage", reason="no_failing_checks")
    elif (
        len(json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode())
        > settings["max_request_bytes"]
    ):
        _mark(rows, "unresolved", "request_too_large")
        report.update(status="unresolved", reason="request_too_large")
    else:
        api_key, credential_reason = auth.request_credential(get_api_key)
        if credential_reason:
            _mark(rows, "unavailable", credential_reason)
            report.update(status="unavailable", reason=credential_reason)
        else:
            attempts = []
            try:
                response = (request or request_jev)(
                    body,
                    api_key=api_key,
                    deadline_ms=settings["deadline_ms"],
                    max_response_bytes=settings["max_response_bytes"],
                    cancel=cancel,
                    on_attempt=attempts.append,
                )
                # Injected callers must meet the same contract as the transport.
                response = validate_jev_response(
                    response, settings["model"], questions, choices={key: options for key in questions}
                )
            except JevError as exc:
                response = None
                status = (
                    "invalid" if exc.code in ("invalid_response", "response_too_large") else "unavailable"
                )
                _mark(rows, status, exc.code)
                report.update(status=status, reason=exc.code)
            except Exception:  # noqa: BLE001 - injected provider errors must not expose secrets
                response = None
                _mark(rows, "unavailable", "network_error")
                report.update(status="unavailable", reason="network_error")
            report["attempts"] = len(attempts)
    if response is not None:
        # Configuration changed or disabled mid-request: discard the answers.
        try:
            current = triage_settings(load_config(root))
            changed = not current["enabled"] or digest(current) != digest(settings)
        except (FactoryError, OSError):
            changed = True
        if changed:
            _mark(rows, "unavailable", "configuration_changed")
            report.update(status="unavailable", reason="configuration_changed")
        else:
            for index, row in enumerate(rows):
                answer = response["answers"]["q" + str(index)]
                accepted = answer["choice"] != "abstain" and answer["confidence"] >= threshold
                row.update(
                    status="evaluated",
                    reason=None
                    if accepted
                    else ("abstained" if answer["choice"] == "abstain" else "low_confidence"),
                    choice=answer["choice"],
                    probabilities=answer["probabilities"],
                    confidence=answer["confidence"],
                    category=answer["choice"] if accepted else None,
                    review="classified" if accepted else "unclassified",
                )
            report.update(returned_model=response["model"], usage=response["usage"])
    report["coverage"] = {
        "failing": len(failing),
        "expected": len(rows),
        "omitted": len(omitted),
        **{
            name: sum(row["status"] == name for row in rows)
            for name in ("evaluated", "unresolved", "unavailable", "invalid")
        },
    }
    report["elapsed_ms"] = round((time.monotonic() - started) * 1000)
    _private(root)
    relative = f"{LOCAL}/{mission}-{revision}-{uuid.uuid4()}.json"
    safe_path(root, LOCAL).mkdir(parents=True, exist_ok=True, mode=0o700)
    data = (json.dumps(report, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()
    write_bytes(root, relative, data)
    exit_code = 2 if report["status"] in ("unavailable", "invalid") else 0
    if settings["claim_mode"] == "shadow":
        # Shadow output withholds categories, confidences and review counts.
        result = {
            **{
                key: report[key]
                for key in (
                    "schema_version",
                    "operation",
                    "status",
                    "reason",
                    "mode",
                    "advisory_only",
                    "coverage",
                    "elapsed_ms",
                    "usage",
                    "attempts",
                )
            },
            "judgments_withheld": True,
            "report": relative,
            "report_hash": sha256(data),
        }
    else:
        result = {
            **{
                key: report[key]
                for key in (
                    "schema_version",
                    "operation",
                    "status",
                    "reason",
                    "mode",
                    "advisory_only",
                    "mission_id",
                    "revision",
                    "triage_accept_confidence",
                    "coverage",
                    "omitted_checks",
                    "elapsed_ms",
                    "usage",
                    "attempts",
                )
            },
            "checks": [
                {
                    "check": row["check"],
                    "category": row["category"],
                    "confidence": row["confidence"],
                    "review": row["review"],
                    **({"reason": row["reason"]} if row["reason"] else {}),
                }
                for row in rows
            ],
            "record": relative,
            "report_hash": sha256(data),
        }
    if exit_code:
        result["_exit_code"] = exit_code
    return result


def handler(args):
    cancel, previous = threading.Event(), {}
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, lambda *_: cancel.set())
    try:
        return triage_checks(
            args.root,
            args.mission,
            args.revision,
            check=args.check,
            no_network=args.no_network,
            cancel=cancel,
        )
    finally:
        for signum, previous_handler in previous.items():
            signal.signal(signum, previous_handler)


def add_parser(subparsers):
    parser = subparsers.add_parser(
        "triage",
        help="Explain check failures with JEV (advisory; off by default)",
    )
    parser.add_argument("--mission", required=True, metavar="ID", help="Mission ID")
    parser.add_argument(
        "--revision", required=True, metavar="REV", help="Verified revision whose failed checks to explain"
    )
    parser.add_argument("--check", metavar="ID", help="Explain only this check")
    parser.add_argument(
        "--no-network", action="store_true", help="Make no provider request (reports network_disallowed)"
    )
    parser.set_defaults(handler=handler)
    return parser
