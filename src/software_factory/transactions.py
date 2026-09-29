"""Preimage-checked, journaled project file transactions."""

from __future__ import annotations

import base64
import json
import os
from contextlib import contextmanager
from pathlib import Path

from .core import FactoryError, git, now, process_alive, safe_path, sha256, write_bytes, write_json

JOURNAL = ".factory/local/installation-transaction.json"
LOCK = ".factory-install.lock"


def optional(root: Path, name: str) -> bytes | None:
    path = safe_path(root, name)
    if path.exists() and not path.is_file():
        raise FactoryError(f"Expected a regular file: {name}")
    return path.read_bytes() if path.exists() else None


def ensure_no_journal(root: Path) -> None:
    """Refuse new project writes while an interrupted operation awaits recovery."""
    if safe_path(root, JOURNAL).exists():
        raise FactoryError(
            "An interrupted factory operation left a recovery journal; run software-factory recover "
            "(then recover --apply) before another write"
        )


def encoded(data: bytes | None) -> str | None:
    return base64.b64encode(data).decode() if data is not None else None


def decoded(data: str | None) -> bytes | None:
    return base64.b64decode(data, validate=True) if data is not None else None


@contextmanager
def operation_lock(root: Path):
    lock = safe_path(root, LOCK)
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise FactoryError(
            "Installation lock exists. Inspect the recorded process; use recover after it stops."
        ) from exc
    try:
        os.write(fd, f"{os.getpid()}\n".encode())
        os.close(fd)
        yield
    finally:
        lock.unlink(missing_ok=True)


def _replace(root: Path, name: str, value: bytes | None, mode: int = 0o644):
    if value is None:
        safe_path(root, name).unlink(missing_ok=True)
    else:
        write_bytes(root, name, value, mode)


def apply(
    root: Path,
    changes: dict[str, bytes | None],
    *,
    expected: dict[str, bytes | None] | None = None,
    label="update",
) -> dict:
    root = Path(root)
    before = expected if expected is not None else {name: optional(root, name) for name in changes}
    operations = {name: data for name, data in changes.items() if before[name] != data}
    if not operations:
        return {"changed": [], "operation": label}
    ensure_no_journal(root)
    for name in operations:
        if optional(root, name) != before[name]:
            raise FactoryError(f"Target changed since planning: {name}")
    with operation_lock(root):
        journal = {
            "schema_version": 1,
            "operation": label,
            "started_at": now(),
            "pid": os.getpid(),
            "files": {},
        }
        for name, data in operations.items():
            path = safe_path(root, name)
            journal["files"][name] = {
                "before": encoded(before[name]),
                "after": encoded(data),
                "mode": path.stat().st_mode & 0o777 if path.exists() else 0o644,
            }
        # A nested deny-all ignore is established before backup bytes exist. It
        # remains after rollback, including when initial root ignores roll back.
        private_ignore = safe_path(root, ".factory/local/.gitignore")
        if private_ignore.exists() and private_ignore.read_bytes() != b"*\n":
            raise FactoryError("Private journal ignore rules need reconciliation before writing backups")
        write_bytes(root, ".factory/local/.gitignore", b"*\n")
        try:
            tracked = git(root, "ls-files", "--", ".factory/local", check=False)
        except FactoryError:
            tracked = ""  # No Git executable: nothing can be tracked.
        if tracked.strip():
            raise FactoryError("Tracked private files prevent installation journals; resolve explicitly")
        write_json(root, JOURNAL, journal)
        completed = []
        try:
            for name, data in operations.items():
                if optional(root, name) != before[name]:
                    raise FactoryError(f"Target changed during apply: {name}")
                completed.append(name)
                _replace(root, name, data, journal["files"][name]["mode"])
        except BaseException:
            conflicts = []
            for name in reversed(completed):
                try:
                    current = optional(root, name)
                    if current == operations[name]:
                        _replace(root, name, before[name], journal["files"][name]["mode"])
                    elif current != before[name]:
                        conflicts.append(name)
                except (OSError, FactoryError):
                    conflicts.append(name)
            if not conflicts:
                safe_path(root, JOURNAL).unlink(missing_ok=True)
            raise
        safe_path(root, JOURNAL).unlink(missing_ok=True)
    return {"changed": sorted(operations), "operation": label}


def _journal_record(name, record) -> tuple[bytes | None, bytes | None, int]:
    """Decode one journal entry, refusing anything that is not a well-formed record."""
    if not isinstance(record, dict) or set(record) != {"before", "after", "mode"}:
        raise FactoryError(f"Invalid installation recovery journal entry: {name}")
    mode = record["mode"]
    if not isinstance(mode, int) or isinstance(mode, bool) or not 0 <= mode <= 0o777:
        raise FactoryError(f"Invalid installation recovery journal mode: {name}")
    values = []
    for key in ("before", "after"):
        if record[key] is not None and not isinstance(record[key], str):
            raise FactoryError(f"Invalid installation recovery journal entry: {name}")
        try:
            values.append(decoded(record[key]))
        except ValueError as exc:
            raise FactoryError(f"Invalid installation recovery journal entry: {name}") from exc
    return values[0], values[1], mode


def recover(root: Path, *, apply_recovery=False) -> dict:
    from .rendering import allowed_export

    journal_path = safe_path(root, JOURNAL)
    lock = safe_path(root, LOCK)
    if lock.exists():
        try:
            pid = int(lock.read_text().strip())
            alive = process_alive(pid)
        except (ValueError, OSError, FactoryError) as exc:
            raise FactoryError(
                "Cannot establish that the installation owner has stopped; inspect lock manually"
            ) from exc
        if alive:
            raise FactoryError(f"Installation process {pid} still exists; recovery refused")
    if not journal_path.exists():
        stale_lock = lock.exists()
        if apply_recovery:
            lock.unlink(missing_ok=True)
        return {"status": "no_journal", "stale_lock": stale_lock, "dry_run": not apply_recovery}
    try:
        raw = journal_path.read_bytes()
        journal = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise FactoryError(f"Invalid installation recovery journal: {exc}") from exc
    if (
        not isinstance(journal, dict)
        or journal.get("schema_version") != 1
        or not isinstance(journal.get("files"), dict)
    ):
        raise FactoryError("Invalid installation recovery journal")
    records = {}
    for name, record in journal["files"].items():
        safe_path(root, name)
        permitted = (
            name in ("factory.json", "factory.lock.json", ".gitignore")
            or allowed_export(name)
            or (
                name.startswith(".factory/")
                and not name.startswith((".factory/local/", ".factory/missions/", ".factory/.venv/"))
            )
        )
        if not permitted:
            raise FactoryError(f"Invalid recovery path: {name}")
        records[name] = _journal_record(name, record)
    changes = {}
    conflicts = []
    for name, (before, after, _mode) in records.items():
        current = optional(root, name)
        if current == after:
            changes[name] = before
        elif current != before:
            conflicts.append(name)
    if conflicts:
        raise FactoryError("Recovery preserves concurrent edits; resolve: " + ", ".join(conflicts))
    if apply_recovery:
        lock.unlink(missing_ok=True)
        with operation_lock(root):
            for name, value in changes.items():
                _before, after, mode = records[name]
                if optional(root, name) != after:
                    raise FactoryError(f"Changed during recovery: {name}")
                _replace(root, name, value, mode)
            journal_path.unlink()
    return {
        "status": "recovered" if apply_recovery else "planned",
        "dry_run": not apply_recovery,
        "restored": sorted(changes),
        "journal_sha256": sha256(raw),
    }


def _factory_owned(rel: str, directory: bool) -> bool:
    """Whether a path is (or may contain) a factory-owned file rather than user content."""
    from .rendering import allowed_export

    return (
        rel.startswith(".factory/")
        or allowed_export(rel)
        or (directory and allowed_export(rel + "/SKILL.md"))
    )


def planning_snapshot(root: Path) -> dict[str, bytes]:
    """Capture consumed project inputs before planning; private/runtime state excluded."""
    result = {}
    for name in (
        "factory.json",
        "factory.lock.json",
        ".gitignore",
        "AGENTS.md",
        "CLAUDE.md",
        ".github/copilot-instructions.md",
        ".codex/config.toml",
        ".claude/settings.json",
    ):
        value = optional(root, name)
        if value is not None:
            result[name] = value
    for directory in (
        ".factory",
        ".claude/agents",
        ".claude/skills",
        ".agents/skills",
        ".codex/agents",
        ".github/agents",
        ".github/skills",
    ):
        base = safe_path(root, directory)
        if not base.exists():
            continue
        for current, dirs, names in os.walk(base, followlinks=False):
            kept = []
            for name in dirs:
                if name in (".venv", "local", "missions", "node_modules", "__pycache__"):
                    continue
                path = Path(current) / name
                rel = path.relative_to(root).as_posix()
                # A user's symlinked skill or agent directory is not factory input; only a
                # symlink where the factory owns (or will write) the path is refused.
                if path.is_symlink() and not _factory_owned(rel, True):
                    continue
                safe_path(root, rel)
                kept.append(name)
            dirs[:] = kept
            for name in names:
                path = Path(current) / name
                rel = path.relative_to(root).as_posix()
                if path.is_symlink() and not _factory_owned(rel, False):
                    continue
                value = optional(root, rel)
                if value is not None:
                    result[rel] = value
    return result


def planned_preimages(root: Path, snapshot: dict[str, bytes], changes: dict) -> dict:
    for name, before in snapshot.items():
        if optional(root, name) != before:
            raise FactoryError(f"Target changed during planning: {name}")
    expected = {name: snapshot.get(name) for name in changes}
    for name, before in expected.items():
        if optional(root, name) != before:
            raise FactoryError(f"Target changed during planning: {name}")
    return expected
