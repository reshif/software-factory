"""Content-bound candidate identity and transient filesystem mutation guards.

Local evidence does not authenticate approvals or remote CI. Git's index stat
cache is never trusted when comparing the files that checks actually execute.
"""

from __future__ import annotations

import ctypes
import errno
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

from .core import (
    FactoryError,
    digest,
    git,
    hash_file,
    read_json,
    runtime_fingerprint,
    safe_path,
    sha256,
)

# Accepted `evidence_exclude` spellings. ".factory/missions/" denotes only the
# record layout the factory itself writes (MISSION_RECORD), never the whole tree.
EXCLUDED = (".factory/missions/", ".factory/local/", "factory.lock.json")
_ID = r"[A-Za-z][A-Za-z0-9_-]{0,79}"
MISSION_RECORD = re.compile(
    rf"\.factory/missions/{_ID}/(?:mission\.json|spec\.md|plan\.md|decisions\.md|handoff\.md|recovery\.md"
    r"|request\.md|clarifications\.md|context\.md"
    r"|pull-request\.md|(?:handoff|release|recovery)-packet\.md|results/index\.json"
    rf"|results/records/{_ID}-[0-9a-f]{{32}}\.json|evidence/{_ID}/checks\.json|models/{_ID}\.json)"
)
# Result directories (created while record_results is monitored) and
# write_bytes() temporaries of the record layout are metadata only for event
# classification; a leftover temporary still enters the fingerprint. Creating
# any other directory under .factory/missions during monitoring is a change.
MISSION_RECORD_DIRECTORY = re.compile(rf"\.factory/missions/{_ID}/results(?:/records)?")
INSTRUCTIONS = ("AGENTS.md", "AGENTS.override.md", "CLAUDE.md", "CLAUDE.local.md")
GOVERNED_DIRECTORIES = (
    ".factory/src",
    ".factory/schemas",
    ".factory/roles",
    ".factory/skills",
    ".factory/prompts",
    ".factory/models",
    ".factory/vendors",
    ".factory/hooks",
    ".factory/templates",
    ".factory/docs",
    ".claude",
    ".codex",
    ".agents",
    ".github",
    ".vscode",
)
GOVERNED_FILES = (
    ".factory/CONSTITUTION.md",
    "factory.json",
    ".gitignore",
    ".gitattributes",
    ".factory/policy.json",
    ".factory/workflow.json",
    ".factory/registry.json",
    ".factory/installation.json",
    ".factory/pyproject.toml",
    ".factory/uv.lock",
    ".factory/run.py",
)
CACHE_DIRECTORIES = frozenset(
    (
        ".git",
        "node_modules",
        ".venv",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
    )
)


def is_metadata(file):
    return (
        file.startswith(".factory/local/")
        or file == "factory.lock.json"
        or MISSION_RECORD.fullmatch(file) is not None
    )


def is_mission_record_event(file, directory):
    if is_metadata(file) or (directory and MISSION_RECORD_DIRECTORY.fullmatch(file)):
        return True
    parent, _, name = file.rpartition("/")
    temporary = re.fullmatch(r"\.(.+)\.[a-z0-9_]{8}", name)
    return not directory and bool(temporary and is_metadata(f"{parent}/{temporary.group(1)}"))


def matches_path(file, pattern):
    if (
        not isinstance(pattern, str)
        or not pattern
        or pattern.startswith("/")
        or "\\" in pattern
        or ".." in pattern.split("/")
        or "\x00" in pattern
    ):
        raise FactoryError(f"Unsafe path pattern: {pattern!r}")
    if pattern.endswith("/"):
        # A trailing-slash directory pattern ("tests/") covers everything below it.
        pattern += "**"
    parts = re.split(r"(\*\*/|\*\*|\*)", pattern)

    expression = "".join({"**/": "(?:.*/)?", "**": ".*", "*": "[^/]*"}.get(p, re.escape(p)) for p in parts)
    return re.fullmatch(expression, file) is not None


def _git_bytes(root, *args, data=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            input=data,
            capture_output=True,
            check=False,
            timeout=30,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FactoryError(f"Git content inspection failed: {exc}") from exc
    if result.returncode:
        raise FactoryError(result.stderr.decode(errors="replace").strip())
    return result.stdout


def _collect_sources(root):
    root = Path(root)
    hidden = [
        entry[2:]
        for entry in git(root, "ls-files", "-v", "-z").split("\0")
        if entry and (entry[0] == "S" or entry[0].islower())
    ]
    if hidden:
        raise FactoryError("Index marks paths skip-worktree or assume-unchanged: " + ", ".join(hidden[:10]))
    visible = {
        p
        for p in git(root, "ls-files", "-z", "--cached", "--others", "--exclude-standard").split("\0")
        if p and not is_metadata(p)
    }
    governed = set(GOVERNED_FILES)
    for directory in GOVERNED_DIRECTORIES:
        target = safe_path(root, directory)
        if not target.exists():
            continue
        for location, dirs, names in os.walk(target, followlinks=False):
            dirs[:] = [d for d in dirs if d not in CACHE_DIRECTORIES]
            for name in names:
                if name.endswith(".pyc"):
                    continue
                governed.add((Path(location) / name).relative_to(root).as_posix())
            for name in dirs:
                if (Path(location) / name).is_symlink():
                    raise FactoryError(f"Symlink in governance directory: {location}/{name}")

    # Ignored instruction files still govern work. Discover them on every
    # snapshot, including directories first created after mission creation.
    def inaccessible(error):
        raise FactoryError(f"Cannot inspect candidate instructions: {error}") from error

    for location, dirs, names in os.walk(root, followlinks=False, onerror=inaccessible):
        dirs[:] = [
            name
            for name in dirs
            if name not in CACHE_DIRECTORIES
            and (Path(location) / name).relative_to(root).as_posix() != ".factory/local"
        ]
        for name in names:
            if name in INSTRUCTIONS:
                governed.add((Path(location) / name).relative_to(root).as_posix())
    directories = {""}
    for file in visible | governed:
        for parent in Path(file).parents:
            directories.add("" if str(parent) == "." else parent.as_posix())
    for directory in directories:
        governed.update(f"{directory}/{n}" if directory else n for n in INSTRUCTIONS)
    return visible, governed


def _entries(root, files):
    result = []
    for file in sorted(files):
        target = safe_path(root, file)
        if not target.exists():
            result.append([file, "missing"])
            continue
        if not target.is_file():
            raise FactoryError(f"Cannot fingerprint directory/submodule or special file: {file}")
        result.append(
            [
                file,
                0o755 if target.stat().st_mode & 0o111 else 0o644,
                sha256(target.read_bytes()),
            ]
        )
    return result


def _content_changes(root, base, present):
    baseline = {}
    for row in _git_bytes(root, "ls-tree", "-r", "-z", "--full-tree", base).split(b"\0"):
        if not row:
            continue
        metadata, raw_path = row.split(b"\t", 1)
        name = os.fsdecode(raw_path)
        if not is_metadata(name):
            mode, _, blob = metadata.decode().split(" ")
            baseline[name] = (mode, blob)
    changed = set()
    for file in set(baseline) | set(present):
        target = safe_path(root, file)
        before = baseline.get(file)
        if not target.exists() or before is None:
            changed.add(file)
            continue
        if not target.is_file() or before[0] not in ("100644", "100755"):
            raise FactoryError(f"Cannot fingerprint directory/submodule or special file: {file}")
        if bool(target.stat().st_mode & 0o111) != (before[0] == "100755"):
            changed.add(file)
        # --path applies the same Git clean filters as the index without trusting stat data.
        blob = (
            _git_bytes(root, "hash-object", "--stdin", "--path", file, data=target.read_bytes())
            .decode()
            .strip()
        )
        if blob != before[1]:
            changed.add(file)
    return changed


def _assert_repository(root):
    root = Path(root).resolve()
    if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise FactoryError("Factory root must be the Git repository root")
    return root


def capture_local_governance(root):
    root = Path(root).resolve()
    visible, governed = _collect_sources(root)
    for location, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [
            d
            for d in dirs
            if d not in CACHE_DIRECTORIES
            and (Path(location) / d).relative_to(root).as_posix() != ".factory/local"
        ]
        for name in names:
            if name in INSTRUCTIONS:
                governed.add((Path(location) / name).relative_to(root).as_posix())
    result = {}
    for file in sorted(governed - visible):
        target = safe_path(root, file)
        if target.exists():
            if not target.is_file():
                raise FactoryError(f"Governance path must be a regular file: {file}")
            result[file] = digest([target.stat().st_mode & 0o777, sha256(target.read_bytes())])
    return result


def candidate_snapshot(root, base=None):
    root = _assert_repository(root)
    config = read_json(root, "factory.json")
    if any(exclusion not in EXCLUDED for exclusion in config.get("evidence_exclude", [])):
        raise FactoryError(
            "Source exclusions cannot hide candidate files; only built-in metadata exclusions are supported"
        )
    head = git(root, "rev-parse", "HEAD")
    if base is not None:
        git(root, "cat-file", "-e", f"{base}^{{commit}}")
    visible, governed = _collect_sources(root)
    entries = _entries(root, visible | governed)
    present = {entry[0] for entry in entries if entry[1] != "missing"}
    dirty = sorted(_content_changes(root, head, present))
    changed = sorted(_content_changes(root, base or head, present))
    return {
        "fingerprint": digest({"head": head, "entries": entries, "runtime": runtime_fingerprint()}),
        "head": head,
        "source_paths": sorted(visible | governed),
        "dirty_paths": dirty,
        "changed_paths": changed,
        "unmerged_paths": sorted(
            filter(
                None,
                git(root, "diff", "--name-only", "--diff-filter=U", "-z").split("\0"),
            )
        ),
    }


def fingerprint(root, mission, record_root=None):
    root = _assert_repository(root)
    record_root = Path(record_root or root).resolve()
    candidate = candidate_snapshot(root, mission["base_commit"])
    visible, governed = _collect_sources(root)
    governed.update(mission.get("governance_snapshot", {}))
    entries = _entries(root, visible | governed)
    hidden_changes = []
    for file in sorted(governed - visible):
        target = safe_path(root, file)
        current = (
            digest([target.stat().st_mode & 0o777, sha256(target.read_bytes())]) if target.exists() else None
        )
        if current != mission.get("governance_snapshot", {}).get(file):
            hidden_changes.append(file)
    spec = f".factory/missions/{mission['id']}/spec.md"
    spec_hash = hash_file(record_root, spec)
    contracts = []
    plans = {}
    for task in mission["tasks"]:
        contract = {k: task[k] for k in ("id", "title", "depends_on", "owned_paths", "checks")}
        if "criteria" in task:
            contract["criteria"] = task["criteria"]
        if task.get("model_assignment"):
            from .models import resolve_assignment

            resolve_assignment(record_root, mission, task["model_assignment"])
            contract["model_assignment"] = task["model_assignment"]
            path = task["model_assignment"]["plan_path"]
            plans[path] = hash_file(record_root, path)
        contracts.append(contract)
    # Request-bearing missions bind their acceptance criteria like task contracts;
    # legacy missions keep their 0.2.x fingerprint payload.
    criteria = {"criteria_hash": digest(mission["criteria"])} if "criteria" in mission else {}
    candidate.update(
        {
            "fingerprint_format": "git-mode-v1",
            "base_commit": mission["base_commit"],
            "spec_hash": spec_hash,
            "fingerprint": digest(
                {
                    "head": candidate["head"],
                    "base_commit": mission["base_commit"],
                    "kind": mission["kind"],
                    "governance_snapshot": mission.get("governance_snapshot", {}),
                    "task_contracts": contracts,
                    "entries": entries,
                    "spec_hash": spec_hash,
                    "model_plans": plans,
                    "runtime": runtime_fingerprint(),
                    **criteria,
                }
            ),
            "criteria_hash": criteria.get("criteria_hash"),
            "changed_paths": sorted(
                _content_changes(root, mission["base_commit"], visible) | set(hidden_changes)
            ),
            "source_paths": sorted(set(candidate["source_paths"]) | {spec} | set(plans)),
        }
    )
    return candidate


def _governed_event(file):
    return (
        Path(file).name in INSTRUCTIONS
        or file in GOVERNED_FILES
        or any(file == p or file.startswith(p + "/") for p in GOVERNED_DIRECTORIES)
    ) and not any(p in CACHE_DIRECTORIES for p in Path(file).parts)


# watchdog 6's inotify reader drops IN_Q_OVERFLOW records (wd == -1) and
# ignores watches it cannot add for new directories (EACCES, or an OSError it
# suppresses), so neither reaches the public event API. These process-wide
# hooks count such losses; every monitor alive at the time fails closed. Other
# backends (FSEvents dropped/rescan flags, Windows buffer overflow) are not
# surfaced by watchdog and remain an unobservable limit of native monitoring.
_EVENT_LOSS = {"overflow": 0, "watch": 0}
_EVENT_LOSS_LOCK = threading.Lock()


def _note_event_loss(kind):
    with _EVENT_LOSS_LOCK:
        _EVENT_LOSS[kind] += 1


def _event_loss():
    with _EVENT_LOSS_LOCK:
        return dict(_EVENT_LOSS)


def _install_inotify_hooks():
    """Install the loss-counting hooks; return False only when Linux loss detection is unavailable.

    Non-Linux platforms have no inotify backend (importing it raises
    UnsupportedLibcError on macOS and can fail with TypeError on Windows), so
    there is nothing to hook. On Linux any failure is reported to the caller,
    which fails monitoring closed rather than silently missing overflows.
    """
    if not sys.platform.startswith("linux"):
        return True
    try:
        from watchdog.observers.inotify_c import Inotify, InotifyConstants

        if getattr(Inotify, "_software_factory_hooked", False):
            return True
        parse, raise_error = Inotify._parse_event_buffer, Inotify._raise_error
        overflow = InotifyConstants.IN_Q_OVERFLOW
    except Exception:  # noqa: BLE001 - e.g. UnsupportedLibcError, TypeError, missing private API
        return False

    def parse_event_buffer(event_buffer):
        for wd, mask, cookie, name in parse(event_buffer):
            if wd == -1 and mask & overflow:
                _note_event_loss("overflow")
            yield wd, mask, cookie, name

    def failed_watch():
        # A directory removed before its watch was added lost nothing durable.
        if ctypes.get_errno() not in (errno.ENOENT, errno.ENOTDIR):
            _note_event_loss("watch")
        raise_error()

    Inotify._parse_event_buffer = staticmethod(parse_event_buffer)
    Inotify._raise_error = staticmethod(failed_watch)
    Inotify._software_factory_hooked = True
    return True


class CandidateMonitor:
    """Observe writes, including write-and-restore, with native OS events.

    Failure to start/drain monitoring fails verification closed. Reading files
    produces open/close events on Linux; only actual mutation events count.
    """

    def __init__(self, root, source_paths=(), metadata_prefixes=()):
        self.root = Path(root).resolve()
        self.known = set(source_paths)
        self.metadata_prefixes = tuple(metadata_prefixes)
        self.source_changed = False
        self.monitoring_reasons = []
        self._lock = threading.Lock()
        self._pending = set()
        self._closed = False
        self.observer = None
        self._loss_detection = _install_inotify_hooks()
        self._losses = _event_loss()
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer

            monitor = self

            class Handler(FileSystemEventHandler):
                def on_any_event(self, event):
                    if event.event_type not in (
                        "modified",
                        "created",
                        "deleted",
                        "moved",
                    ):
                        return
                    for raw in (event.src_path, getattr(event, "dest_path", "")):
                        if not raw:
                            continue
                        try:
                            file = Path(raw).relative_to(monitor.root).as_posix()
                        except ValueError:
                            monitor.uncertain("Filesystem monitor observed an out-of-root event")
                            continue
                        if event.is_directory and event.event_type == "modified":
                            continue
                        with monitor._lock:
                            if len(monitor._pending) >= 65536:
                                monitor.monitoring_reasons.append("Filesystem monitor event bound exceeded")
                            else:
                                monitor._pending.add((file, event.is_directory, event.event_type))

            self.observer = Observer()
            self.observer.schedule(Handler(), str(self.root), recursive=True)
            self.observer.start()
        except (ImportError, OSError, RuntimeError) as exc:
            self.uncertain(f"Filesystem monitoring could not start: {exc}")

    def uncertain(self, reason):
        with self._lock:
            if reason not in self.monitoring_reasons:
                self.monitoring_reasons.append(reason)

    def _drain(self):
        with self._lock:
            batch, self._pending = self._pending, set()
        unknown = []
        for file, directory, event in batch:
            if file == ".git" or file.startswith(".git/"):
                continue
            observed = any(file.startswith(p) or p.startswith(file + "/") for p in self.metadata_prefixes)
            if observed or file in self.known or _governed_event(file):
                self.source_changed = True
            elif not is_mission_record_event(file, directory) and file != ".":
                unknown.append((file, directory, event))
        if not unknown:
            return
        try:
            env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
            paths = sorted({file + ("/" if directory else "") for file, directory, _ in unknown})
            result = subprocess.run(
                [
                    "git",
                    "-C",
                    str(self.root),
                    "check-ignore",
                    "--no-index",
                    "--stdin",
                    "-z",
                ],
                input=("\0".join(paths) + "\0").encode(),
                capture_output=True,
                check=False,
                timeout=20,
                env=env,
            )
            if result.returncode not in (0, 1):
                raise FactoryError(result.stderr.decode(errors="replace"))
            ignored = {os.fsdecode(p) for p in result.stdout.split(b"\0") if p}
            if any(p not in ignored for p in paths):
                self.source_changed = True
        except (FactoryError, OSError, subprocess.SubprocessError) as exc:
            self.uncertain(f"Could not classify filesystem events: {exc}")

    def _check_losses(self):
        # Without a recorded start (a partially constructed monitor), any loss counts.
        losses, start = _event_loss(), getattr(self, "_losses", {"overflow": 0, "watch": 0})
        if not getattr(self, "_loss_detection", False):
            self.uncertain("Filesystem event-loss detection is unavailable; changes may have been missed")
        if losses["overflow"] != start["overflow"]:
            self.uncertain("Filesystem event queue overflowed; changes may have been missed")
        if losses["watch"] != start["watch"]:
            self.uncertain("Filesystem monitor could not watch a directory; changes may have been missed")

    def report(self):
        # A closed monitor has already drained; no meaningful work remains in
        # cleanup after a caller has published its final verdict.
        if not self._closed:
            time.sleep(0.08)
        self._drain()
        if (
            self.observer is not None
            and not self._closed
            and (
                not self.observer.is_alive()
                or any(not emitter.is_alive() for emitter in self.observer.emitters)
            )
        ):
            self.uncertain("Filesystem observer stopped before assessment completed")
        if not self._closed:
            self._check_losses()
        return {
            "source_changed": self.source_changed,
            "monitoring_uncertain": bool(self.monitoring_reasons),
            "monitoring_reasons": list(dict.fromkeys(self.monitoring_reasons)),
        }

    def close(self):
        """Stop producers, drain dispatched events, and expose final uncertainty.

        Callers must close before their final content snapshot and verdict.
        Repeated cleanup is intentionally inert.
        """
        if self._closed:
            return self.report()
        self.report()
        if self.observer is not None:
            emitters = tuple(self.observer.emitters)
            try:
                # Keep the event dispatcher running until every producer stops
                # and its pending events have been delivered to our handler.
                for emitter in emitters:
                    emitter.stop()
                for emitter in emitters:
                    emitter.join(timeout=2)
                    if emitter.is_alive():
                        self.uncertain("Filesystem event producer did not stop")
                deadline = time.monotonic() + 2
                while self.observer.event_queue.unfinished_tasks and time.monotonic() < deadline:
                    if not self.observer.is_alive():
                        break
                    time.sleep(0.005)
                if self.observer.event_queue.unfinished_tasks:
                    self.uncertain("Filesystem event queue did not drain before assessment completed")
                self._drain()
            except (OSError, RuntimeError) as exc:
                self.uncertain(f"Filesystem monitor could not finish draining: {exc}")
            finally:
                try:
                    self.observer.stop()
                    self.observer.join(timeout=2)
                    if self.observer.is_alive():
                        self.uncertain("Filesystem observer did not stop")
                except (OSError, RuntimeError) as exc:
                    self.uncertain(f"Filesystem observer shutdown was incomplete: {exc}")
        self._check_losses()
        self._closed = True
        self._drain()
        return self.report()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


_DIFF_OPTIONS = (
    "--no-ext-diff",
    "--no-textconv",
    "--no-color",
    "--no-renames",
    "--full-index",
    "--binary",
    "--diff-algorithm=myers",
    "--indent-heuristic",
    "--src-prefix=a/",
    "--dst-prefix=b/",
    "-U3",
)


def _scratch_git(root, index, *args, codes=(0,)):
    """Run Git against a private temporary index; the repository index is never read or refreshed."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({"GIT_OPTIONAL_LOCKS": "0", "GIT_INDEX_FILE": str(index)})
    config = ("-c", "core.quotePath=true", "-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false")
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *config, *args], capture_output=True, check=False, timeout=60, env=env
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FactoryError(f"Git diff inspection failed: {exc}") from exc
    if result.returncode not in codes:
        raise FactoryError("Git diff inspection failed: " + result.stderr.decode(errors="replace").strip())
    return result.stdout


def candidate_diff(root, base, paths):
    """Content diff of candidate paths against base: a patch plus per-path line counts.

    A fresh temporary index holding the base tree carries no stat cache, so Git
    compares actual working-tree content. Paths absent from base (untracked or
    newly added files) are rendered as additions. Metadata paths are excluded.
    """
    import tempfile

    root = Path(root).resolve()
    paths = sorted({p for p in paths if not is_metadata(p)})
    in_base = {
        os.fsdecode(p)
        for p in _git_bytes(root, "ls-tree", "-r", "-z", "--name-only", "--full-tree", base).split(b"\0")
        if p
    }
    tracked = [p for p in paths if p in in_base]
    added = [p for p in paths if p not in in_base and safe_path(root, p).is_file()]
    patch, stats = b"", {}
    with tempfile.TemporaryDirectory(prefix="sf-diff-") as scratch:
        index = Path(scratch) / "index"
        if tracked:
            _scratch_git(root, index, "read-tree", base)
            specs = [f":(literal){p}" for p in tracked]
            patch = _scratch_git(root, index, "diff", *_DIFF_OPTIONS, "--", *specs)
            for row in _scratch_git(
                root,
                index,
                "diff",
                "--numstat",
                "-z",
                "--no-renames",
                "--no-ext-diff",
                "--no-textconv",
                "--",
                *specs,
            ).split(b"\0"):
                if row:
                    plus, minus, name = row.decode(errors="replace").split("\t", 2)
                    binary = plus == "-"
                    stats[name] = (0 if binary else int(plus), 0 if binary else int(minus), binary)
        for file in added:
            patch += _scratch_git(
                root, index, "diff", "--no-index", *_DIFF_OPTIONS, "--", "/dev/null", file, codes=(0, 1)
            )
            content = safe_path(root, file).read_bytes()
            binary = b"\0" in content
            lines = content.count(b"\n") + (1 if content and not content.endswith(b"\n") else 0)
            stats[file] = (0 if binary else lines, 0, binary)
    return {
        "patch": patch,
        "stats": stats,
        "base": base,
        "deleted": [p for p in tracked if not safe_path(root, p).exists()],
    }
