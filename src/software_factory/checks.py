"""Bounded product subprocesses and revision-bound verification evidence."""

from __future__ import annotations

import os
import queue
import re
import signal
import subprocess
import threading
import time
from pathlib import Path

from .core import (
    FactoryError,
    assert_id,
    hash_file,
    load_config,
    now,
    private_dir,
    read_json,
    safe_path,
    validate,
    write_json,
)
from .evidence import CandidateMonitor, candidate_snapshot, fingerprint


def successful_check(record):
    return (
        bool(record)
        and record["status"] == "pass"
        and record["exit_code"] == 0
        and record["signal"] is None
        and record["truncated"] is False
    )


def run_check(
    root,
    check,
    default_timeout=120,
    max_output_bytes=1048576,
    log_files=None,
    on_output=None,
    capture_bytes=65536,
):
    command = check.get("command")
    if (
        not isinstance(command, list)
        or not command
        or any(not isinstance(a, str) or "\0" in a for a in command)
    ):
        raise FactoryError(f"Invalid argv for {check.get('id')}")
    if not isinstance(max_output_bytes, int) or max_output_bytes < 1 or capture_bytes < 0:
        raise FactoryError("Invalid subprocess output limit")
    cwd = safe_path(root, check.get("cwd", "."))
    timeout = min(default_timeout, check.get("timeout_seconds", default_timeout))
    if timeout <= 0:
        raise FactoryError("Check timeout must be positive")
    started = time.monotonic()
    streams, captured = {}, {"stdout": bytearray(), "stderr": bytearray()}
    status, total, retained, process = None, 0, 0, None
    events = queue.Queue(maxsize=256)
    readers = []
    previous_sigterm = None
    return_code = None

    def stop(why):
        nonlocal status
        status = status or why
        if process is not None:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGKILL)
                else:
                    subprocess.run(
                        ["taskkill", "/pid", str(process.pid), "/T", "/F"],
                        capture_output=True,
                        check=False,
                        timeout=5,
                    )
                    process.kill()
            except (ProcessLookupError, OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass

    def receive(channel, data):
        nonlocal total, retained
        allowed = data[: max(0, max_output_bytes - total)]
        total += len(data)
        preview = allowed[: max(0, capture_bytes - retained)]
        captured[channel].extend(preview)
        retained += len(preview)
        if allowed:
            if channel in streams:
                streams[channel].write(allowed)
            if on_output:
                on_output(channel, allowed)
        if total > max_output_bytes:
            stop("output_limit")

    def read_pipe(channel, stream):
        try:
            while True:
                data = os.read(stream.fileno(), 16384)
                if not data:
                    break
                events.put((channel, data))
        except OSError:
            pass
        finally:
            events.put((channel, None))

    try:
        if threading.current_thread() is threading.main_thread():
            previous_sigterm = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, lambda *_: stop("interrupted"))
        if log_files:
            for channel in ("stdout", "stderr"):
                path = Path(log_files[channel])
                path.parent.mkdir(parents=True, exist_ok=True)
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                streams[channel] = os.fdopen(descriptor, "wb")
        # argv is never passed through a shell; explicit product shell commands remain user configuration.
        # Product checks never receive the factory's own inference credential.
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env={
                **{k: v for k, v in os.environ.items() if k != "TYPESAFE_API_KEY"},
                "SOFTWARE_FACTORY_AUTH_DISABLED": "1",
            },
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
        for channel in ("stdout", "stderr"):
            thread = threading.Thread(
                target=read_pipe, args=(channel, getattr(process, channel)), daemon=True
            )
            thread.start()
            readers.append(thread)
        closed = set()
        stopped_at = None
        while len(closed) < 2:
            elapsed = time.monotonic() - started
            if elapsed >= timeout and status is None:
                # An exited check whose pipes stay open left a background process behind.
                stop("background_process" if process.poll() is not None else "timeout")
            if status is not None:
                stopped_at = stopped_at or time.monotonic()
                if time.monotonic() - stopped_at > 1:
                    break
            try:
                channel, data = events.get(timeout=0.03)
            except queue.Empty:
                continue
            if data is None:
                closed.add(channel)
            else:
                receive(channel, data)
        try:
            return_code = process.wait(
                timeout=max(0.01, timeout - (time.monotonic() - started)) if status is None else 1
            )
        except subprocess.TimeoutExpired:
            stop("timeout")
            return_code = process.wait(timeout=2)
    except KeyboardInterrupt:
        stop("interrupted")
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        stop("error")
        receive("stderr", str(exc).encode())
    finally:
        if previous_sigterm is not None:
            signal.signal(signal.SIGTERM, previous_sigterm)
        if process is not None:
            if process.poll() is None:
                stop("error")
            for channel in ("stdout", "stderr"):
                stream = getattr(process, channel)
                if stream:
                    stream.close()
        for stream in streams.values():
            stream.flush()
            os.fsync(stream.fileno())
            stream.close()
    signame = None
    if return_code is not None and return_code < 0:
        try:
            signame = signal.Signals(-return_code).name
        except ValueError:
            signame = str(-return_code)
    return {
        "id": check["id"],
        "command": command,
        "cwd": check.get("cwd", "."),
        "required": bool(check.get("required")),
        "status": status or ("pass" if return_code == 0 else "fail"),
        "exit_code": return_code if return_code is None or return_code >= 0 else None,
        "signal": signame,
        "duration_ms": round((time.monotonic() - started) * 1000),
        "truncated": total > max_output_bytes,
        "stdout": captured["stdout"].decode(errors="replace"),
        "stderr": captured["stderr"].decode(errors="replace"),
    }


def _execute_suite(root, config, selected, log_root=None, record_root=None):
    setup, results = [], []

    def execute(check, phase):
        paths = None
        if log_root is not None:
            prefix = "setup/" if phase == "setup" else ""
            paths = {
                channel: f"{log_root}/{prefix}{assert_id(check['id'])}.{channel}.log"
                for channel in ("stdout", "stderr")
            }
        result = run_check(
            root,
            check,
            config["limits"]["check_timeout_seconds"],
            check.get(
                "output_limit_bytes",
                config["limits"].get("check_output_bytes", 1048576),
            ),
            log_files={c: safe_path(record_root or root, p) for c, p in paths.items()} if paths else None,
            capture_bytes=0,
        )
        result.pop("stdout")
        result.pop("stderr")
        if paths:
            for channel, path in paths.items():
                result[f"{channel}_log"] = {
                    "path": path,
                    "sha256": hash_file(record_root or root, path),
                }
        return result

    for step in config.get("setup", []):
        item = execute({**step, "required": True}, "setup")
        setup.append(item)
        if not successful_check(item):
            break
    setup_pass = len(setup) == len(config.get("setup", [])) and all(map(successful_check, setup))
    if setup_pass:
        for check in selected:
            result = execute(check, "check")
            results.append(result)
            if result["status"] == "interrupted":
                break
    return setup, results, setup_pass


def run_checks(root, only=None, require_clean=False):
    # Observe configuration reads as well as candidate collection.
    with CandidateMonitor(root) as monitor:
        config = load_config(root)
        selected = config["checks"] if only is None else [c for c in config["checks"] if c["id"] == only]
        if not selected:
            raise FactoryError(f"Unknown check id: {only}")
        before = candidate_snapshot(root)
        monitor.known.update(before["source_paths"])
        if before["unmerged_paths"]:
            raise FactoryError("Unmerged candidate paths: " + ", ".join(before["unmerged_paths"]))
        if require_clean and before["dirty_paths"]:
            raise FactoryError("CI requires a clean committed candidate: " + ", ".join(before["dirty_paths"]))
        setup, results, setup_pass = _execute_suite(root, config, selected)
        monitor.close()
        after = candidate_snapshot(root)
        monitoring = monitor.report()
        changed = monitoring["source_changed"] or before["fingerprint"] != after["fingerprint"]
        required = [c["id"] for c in selected if only is not None or c["required"]]
        passed = (
            setup_pass
            and not changed
            and not monitoring["monitoring_uncertain"]
            and all(successful_check(next((c for c in results if c["id"] == i), None)) for i in required)
        )
        return {
            "revision": before["head"],
            "fingerprint": before["fingerprint"],
            "post_fingerprint": after["fingerprint"],
            **monitoring,
            "source_changed": changed,
            "dirty_paths": before["dirty_paths"],
            "exact_revision": not changed
            and not monitoring["monitoring_uncertain"]
            and not before["dirty_paths"],
            "scope": "full" if only is None else "focused",
            "setup": setup,
            "checks": results,
            "pass": passed,
        }


def next_run_label(directory, label):
    """A suggested unused evidence run label after label (R-1 -> R-2, other -> other-2)."""
    match = re.fullmatch(r"(.*?)(\d+)", label)
    stem, number = (match.group(1), int(match.group(2))) if match else (label + "-", 1)
    while True:
        number += 1
        candidate = f"{stem}{number}"
        if not (directory / candidate).exists():
            return candidate


def verify_mission(root, mission_id, revision, candidate_root=None, reconcile=False, resolution=None):

    from .workflow import (
        HOLD_STATES,
        POST_MERGE_STATES,
        TERMINAL_STATES,
        load_mission,
        update_mission,
    )

    assert_id(mission_id)
    assert_id(revision)
    root, candidate_root = Path(root).resolve(), Path(candidate_root or root).resolve()
    mission = load_mission(root, mission_id)
    held = mission["state"] in HOLD_STATES
    effective = mission.get("previous_state") if held else mission["state"]
    postmerge = effective in POST_MERGE_STATES
    if reconcile and (not held or not postmerge or not isinstance(resolution, str) or not resolution.strip()):
        raise FactoryError(
            "Postmerge reconciliation requires a held postmerge mission and explicit resolution"
        )
    if mission["state"] in TERMINAL_STATES or (held and not reconcile):
        raise FactoryError(f"Cannot verify {mission['state']} mission; reconcile and resume first")
    if not postmerge and candidate_root != root:
        raise FactoryError("--candidate-root is only supported for recorded postmerge CI candidates")
    private_dir(root)
    relative = f".factory/missions/{mission_id}/evidence/{revision}"
    run_dir = safe_path(root, relative)
    run_dir.parent.mkdir(parents=True, exist_ok=True)
    try:
        run_dir.mkdir()
    except FileExistsError as exc:
        raise FactoryError(
            f"Evidence revision already exists: {revision}; --revision is a unique run label, not a git"
            f" revision: pass a new one such as {next_run_label(run_dir.parent, revision)}"
        ) from exc
    logs = f".factory/local/runs/{mission_id}/{revision}"
    try:
        safe_path(root, logs).mkdir(parents=True, exist_ok=False, mode=0o700)
    except FileExistsError as exc:
        raise FactoryError(
            f"Private logs already exist for revision {revision}; use a new revision ID"
        ) from exc
    published = False
    monitor = CandidateMonitor(
        candidate_root,
        metadata_prefixes=(
            f".factory/missions/{mission_id}/spec.md",
            f".factory/missions/{mission_id}/models/",
        ),
    )
    records_monitor = (
        CandidateMonitor(
            root,
            metadata_prefixes=(
                f".factory/missions/{mission_id}/spec.md",
                f".factory/missions/{mission_id}/models/",
            ),
        )
        if root != candidate_root
        else None
    )
    try:
        config = load_config(candidate_root)
        before = fingerprint(candidate_root, mission, record_root=root)
        monitor.known.update(before["source_paths"])
        if before["unmerged_paths"]:
            raise FactoryError("Cannot verify unresolved merge conflicts")
        if postmerge:
            ci = mission.get("delivery", {}).get("ci_ref", {})
            if before["head"] != ci.get("head_sha") or before["fingerprint"] != ci.get("fingerprint"):
                raise FactoryError(
                    "Postmerge verification requires the exact recorded CI candidate in an isolated checkout"
                )
        started = now()
        setup, results, setup_pass = _execute_suite(candidate_root, config, config["checks"], logs, root)
        monitor.close()
        if records_monitor:
            records_monitor.close()
        current = load_mission(root, mission_id)
        after = fingerprint(candidate_root, current, record_root=root)
        monitoring = monitor.report()
        if records_monitor:
            records = records_monitor.report()
            monitoring["source_changed"] |= records["source_changed"]
            monitoring["monitoring_uncertain"] |= records["monitoring_uncertain"]
            monitoring["monitoring_reasons"] += records["monitoring_reasons"]
        evidence = {
            "schema_version": 1,
            "id": revision,
            "mission_id": mission_id,
            "fingerprint_format": "git-mode-v1",
            "fingerprint": before["fingerprint"],
            "post_fingerprint": after["fingerprint"],
            "head": before["head"],
            "base_commit": before["base_commit"],
            "spec_hash": before["spec_hash"],
            # Registered position; selection rejects evidence whose list order disagrees.
            "sequence": len(mission["evidence"]) + 1,
            "started_at": started,
            "finished_at": now(),
            **monitoring,
            "source_changed": monitoring["source_changed"]
            or current != mission
            or before["fingerprint"] != after["fingerprint"],
            "checks": results,
            "trust": "local-unattested",
            "model_assignments": model_assignments(mission),
        }
        if config.get("setup"):
            evidence["setup"] = setup
        if reconcile:
            evidence["reconciliation"] = {
                "held_state": mission["state"],
                "resolution": resolution.strip(),
            }
        validate(root, "evidence", evidence)
        reference = f"{relative}/checks.json"
        write_json(root, reference, evidence)
        published = True

        # An interrupted check (Ctrl-C/SIGTERM) is not a verdict on the candidate and
        # does not consume a repair attempt; a timeout is a failure and does.
        failed = {c["id"] for c in results if not successful_check(c) and c["status"] != "interrupted"}

        def register(value):
            if value != mission:
                raise FactoryError(
                    "Mission changed during verification; evidence was saved but not registered"
                )
            value["evidence"].append(reference)
            # Repair budget (constitution rule 15): failing a task's checks after it
            # left RUNNING consumes its attempt. It cannot become DONE again until it
            # re-enters RUNNING, which counts a new attempt. Failures while RUNNING
            # belong to the attempt in progress; setup/monitoring failures flag nothing.
            if not postmerge:
                for task in value["tasks"]:
                    if task["status"] in {"VERIFYING", "DONE"} and failed.intersection(task["checks"]):
                        task["repair_required"] = reference

        update_mission(root, mission_id, register)
        required = {c["id"] for c in config["checks"] if c["required"]} | {
            c for t in mission["tasks"] for c in t["checks"]
        }
        passed = (
            setup_pass
            and not evidence["source_changed"]
            and not evidence["monitoring_uncertain"]
            and all(successful_check(next((c for c in results if c["id"] == i), None)) for i in required)
        )
        return {"pass": passed, "reference": reference, **evidence}
    finally:
        monitor.close()
        if records_monitor:
            records_monitor.close()
        if not published:
            try:
                run_dir.rmdir()
            except OSError:
                pass


def model_assignments(mission):
    return [
        {
            "task_id": t["id"],
            "assignment_hash": t["model_assignment"]["assignment_hash"],
        }
        for t in mission["tasks"]
        if t.get("model_assignment")
    ]


def validate_verification(root, mission, config, candidate, tasks=None, reference=None, check_logs=True):
    reasons, evidence = [], None
    reference = reference or next(iter(mission["evidence"][-1:]), None)
    try:
        if not reference or not reference.startswith(f".factory/missions/{mission['id']}/evidence/"):
            raise FactoryError("No verification evidence registered in this mission")
        evidence = validate(root, "evidence", read_json(root, reference))
        if evidence["mission_id"] != mission["id"]:
            raise FactoryError("Evidence mission mismatch")
        if (
            reference not in mission["evidence"]
            or evidence["sequence"] != mission["evidence"].index(reference) + 1
        ):
            raise FactoryError("Evidence registration order disagrees with its recorded sequence")
        if evidence.get("fingerprint_format") != candidate.get("fingerprint_format"):
            reasons.append("Verification fingerprint format differs from accepted candidate")
        if evidence.get("model_assignments", []) != model_assignments(mission):
            reasons.append("Verification model assignments differ from task contracts")
        if (
            evidence["fingerprint"] != candidate["fingerprint"]
            or evidence["post_fingerprint"] != candidate["fingerprint"]
        ):
            reasons.append("Verification evidence is stale for the current candidate")
        if evidence["source_changed"]:
            reasons.append("Source changed during verification")
        if evidence.get("monitoring_uncertain"):
            reasons.append(
                "Candidate monitoring was uncertain: " + "; ".join(evidence.get("monitoring_reasons", []))
            )
        if (
            evidence["spec_hash"] != candidate["spec_hash"]
            or evidence["head"] != candidate["head"]
            or evidence["base_commit"] != mission["base_commit"]
        ):
            reasons.append("Evidence revision/specification metadata mismatch")
        for phase, configured, actual in (
            ("check", config["checks"], evidence["checks"]),
            ("setup", config.get("setup", []), evidence.get("setup", [])),
        ):
            seen = set()
            for item in actual:
                if item["id"] in seen:
                    reasons.append(f"Duplicate {phase} evidence {item['id']}")
                seen.add(item["id"])
                expected = next((c for c in configured if c["id"] == item["id"]), None)
                if (
                    not expected
                    or item["command"] != expected["command"]
                    or item["cwd"] != expected.get("cwd", ".")
                    or item["required"] != (True if phase == "setup" else expected["required"])
                ):
                    reasons.append(f"{phase} {item['id']} differs from current command configuration")
                if check_logs:
                    for channel in ("stdout", "stderr"):
                        log = item[f"{channel}_log"]
                        prefix = "setup/" if phase == "setup" else ""
                        path = f".factory/local/runs/{mission['id']}/{evidence['id']}/{prefix}{item['id']}.{channel}.log"
                        if log["path"] != path or hash_file(root, path) != log["sha256"]:
                            reasons.append(f"{phase} {item['id']} {channel} log does not match evidence")
            required = {c["id"] for c in configured if phase == "setup" or c["required"]}
            if phase == "check":
                required |= {
                    i for task in (mission["tasks"] if tasks is None else tasks) for i in task["checks"]
                }
            for id in required:
                if not successful_check(next((r for r in actual if r["id"] == id), None)):
                    reasons.append(f"Required {phase} {id} did not run successfully")
    except (FactoryError, OSError, KeyError, TypeError) as exc:
        reasons.append(f"Invalid verification evidence: {exc}")
    return {"reasons": reasons, "evidence": evidence, "reference": reference}
