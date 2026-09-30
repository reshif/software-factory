"""Content-bound candidate identity and transient filesystem mutation guards.

Local evidence does not authenticate approvals or remote CI. Git's index stat
cache is never trusted when comparing the files that checks actually execute,
and neither are Git clean filters, textconv or diff attributes: content is
hashed and diffed unfiltered, and the local Git view (info/exclude,
info/attributes, excludes/attributes files, filter.* and diff.*.textconv) is
bound into the fingerprint.

Limits: candidates hold UTF-8-named regular files only (symlinks, submodules
and nested repositories are refused with guidance). Reads of Git-ignored files
cannot be observed; ignored writes are recorded. In a linked worktree (.git is
a file) the Git control files live outside the monitored root. Writes through
another hard link are detected from inode ctime (mtime on Windows).
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import re
import subprocess
import sys
import threading
import time
import zlib
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
    r"|request\.md|clarifications\.md|context\.md|assessment\.md|crew-context\.md|events\.jsonl"
    r"|pull-request\.md|(?:handoff|release|recovery)-packet\.md|results/index\.json"
    rf"|results/records/{_ID}-[0-9a-f]{{32}}\.json|evidence/{_ID}/checks\.json|models/{_ID}\.json)"
)
# Result directories (created while record_results is monitored) and
# write_bytes() temporaries of the record layout are metadata only for event
# classification; a leftover temporary still enters the fingerprint. Creating
# any other directory under .factory/missions during monitoring is a change.
MISSION_RECORD_DIRECTORY = re.compile(rf"\.factory/missions/{_ID}/results(?:/records)?")
# Files any coding client reads as instructions or agent configuration wherever they sit;
# they are governed in every directory, ignored or not.
INSTRUCTIONS = ("AGENTS.md", "AGENTS.override.md", "CLAUDE.md", "CLAUDE.local.md", "GEMINI.md", ".mcp.json")
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
    ".factory/crew",
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
        or any(part in (".", "..") for part in pattern.split("/"))
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


def printable(path):
    """A path as text that always encodes as UTF-8 (undecodable bytes become escapes)."""
    try:
        path.encode("utf-8")
        return path
    except UnicodeEncodeError:
        return os.fsencode(path).decode("utf-8", "backslashreplace")


def _decode_path(raw):
    """Decode a Git path; candidate paths must be UTF-8 so records and digests can hold them."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise FactoryError(
            f"Candidate path is not valid UTF-8: {printable(os.fsdecode(raw))}; rename it to a UTF-8 name"
        ) from None


def _require_utf8(path):
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        raise FactoryError(
            f"Candidate path is not valid UTF-8: {printable(path)}; rename it to a UTF-8 name"
        ) from None
    return path


def _git_paths(root, *args):
    return {_decode_path(p) for p in _git_bytes(root, *args).split(b"\0") if p}


def _tree(root, commit):
    """Non-metadata paths of a commit's tree as {path: (mode, object id)}."""
    result = {}
    for row in _git_bytes(root, "ls-tree", "-r", "-z", "--full-tree", commit).split(b"\0"):
        if not row:
            continue
        metadata, raw_path = row.split(b"\t", 1)
        name = _decode_path(raw_path)
        if not is_metadata(name):
            mode, _, blob = metadata.decode().split(" ")
            result[name] = (mode, blob)
    return result


def _collect_sources(root, tracked=()):
    """Candidate (visible) and governance paths.

    tracked holds the base and HEAD tree paths: a path committed there stays
    part of the candidate even after it is removed from the index and ignored.
    """
    root = Path(root)
    hidden = [
        _decode_path(entry[2:])
        for entry in _git_bytes(root, "ls-files", "-v", "-z").split(b"\0")
        if entry and (entry[:1] == b"S" or entry[:1].islower())
    ]
    if hidden:
        raise FactoryError("Index marks paths skip-worktree or assume-unchanged: " + ", ".join(hidden[:10]))
    cached = _git_paths(root, "ls-files", "-z", "--cached")
    others = _git_paths(root, "ls-files", "-z", "--others", "--exclude-standard")
    visible = {p for p in cached | others | set(tracked) if not is_metadata(p)}
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
        _require_utf8(file)
        for parent in Path(file).parents:
            directories.add("" if str(parent) == "." else parent.as_posix())
    for directory in directories:
        governed.update(f"{directory}/{n}" if directory else n for n in INSTRUCTIONS)
    return visible, governed, cached


def _object_id(data, algorithm):
    """The Git blob id of data, computed without any Git filter or attribute (hash-object --no-filters)."""
    value = hashlib.new(algorithm)
    value.update(b"blob %d\0" % len(data))
    value.update(data)
    return value.hexdigest()


def _is_binary(data):
    # Git's own heuristic (buffer_is_binary): a NUL byte within the first 8000 bytes.
    return b"\0" in data[:8000]


def _candidate_file(root, file):
    """The regular file behind a candidate path, or None when it is absent."""
    if (Path(root) / file).is_symlink():
        raise FactoryError(
            f"Symlink in candidate: {file}; the factory fingerprints regular files only, so replace the"
            " link with a regular file or remove it (symlinks are not supported in candidates)"
        )
    target = safe_path(root, file)
    if not target.exists():
        return None
    if not target.is_file():
        raise FactoryError(
            f"Cannot fingerprint directory/submodule or special file: {file}; the factory fingerprints"
            " regular files only (submodules and nested repositories are not supported in candidates)"
        )
    return target


def _read_files(root, files, algorithm):
    """Fingerprint entries plus, per present file, (executable, raw blob id, CRLF-normalized blob id).

    Each file is read once. Blob ids are computed without Git clean filters,
    so a filter configured in .git/config or .git/info/attributes cannot make
    changed content look committed. Only Git's built-in end-of-line conversion
    is tolerated: a text file equal to its blob after CRLF -> LF is unchanged.
    """
    entries, blobs = [], {}
    for file in sorted(files):
        target = _candidate_file(root, file)
        if target is None:
            entries.append([file, "missing"])
            continue
        data = target.read_bytes()
        executable = bool(target.stat().st_mode & 0o111)
        entries.append([file, 0o755 if executable else 0o644, sha256(data)])
        normalized = (
            _object_id(data.replace(b"\r\n", b"\n"), algorithm)
            if b"\r\n" in data and not _is_binary(data)
            else None
        )
        blobs[file] = (executable, _object_id(data, algorithm), normalized)
    return entries, blobs


def _content_changes(baseline, blobs):
    """Paths whose working-tree content or executable bit differs from the baseline tree."""
    changed = set()
    for file in set(baseline) | set(blobs):
        before, current = baseline.get(file), blobs.get(file)
        if before is None or current is None:
            changed.add(file)
            continue
        if before[0] not in ("100644", "100755"):
            raise FactoryError(
                f"Cannot fingerprint directory/submodule or special file: {file}; the factory fingerprints"
                " regular files only (symlinks and submodules are not supported in candidates)"
            )
        executable, blob, normalized = current
        if executable != (before[0] == "100755") or before[1] not in (blob, normalized):
            changed.add(file)
    return changed


def _assert_repository(root):
    root = Path(root).resolve()
    if Path(git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise FactoryError("Factory root must be the Git repository root")
    return root


def _governance_digest(target):
    # Hidden governance files bind their full permission bits, unlike candidate
    # entries (which bind only the executable bit, as Git does). Mission
    # governance snapshots and the live comparison both use this one form.
    return digest([target.stat().st_mode & 0o777, sha256(target.read_bytes())])


def _git_file(root, name):
    path = Path(_git_bytes(root, "rev-parse", "--git-path", name).decode().strip())
    return path if path.is_absolute() else Path(root) / path


def _file_hash(target):
    try:
        return sha256(Path(target).read_bytes()) if Path(target).is_file() else None
    except OSError as exc:
        raise FactoryError(f"Cannot read Git view file {target}: {exc}") from exc


GIT_VIEW_KEYS = re.compile(r"core\.(?:excludesfile|attributesfile)|filter\..+|diff\..+\.textconv")


def git_view(root):
    """Digest of the local Git settings that change which files Git shows and how it hashes them.

    Binds .git/info/exclude, .git/info/attributes, the effective excludes and
    attributes files, and every core.excludesFile, core.attributesFile,
    filter.* and diff.*.textconv setting from all configuration scopes.
    """
    root = Path(root)
    settings = []
    for item in _git_bytes(root, "config", "--list", "-z").split(b"\0"):
        key, _, value = item.partition(b"\n")
        key = key.decode(errors="replace")
        if item and GIT_VIEW_KEYS.fullmatch(key.lower()):
            settings.append([key, value.decode(errors="replace")])
    configured = {key.lower(): value for key, value in settings}
    files = {name: _file_hash(_git_file(root, name)) for name in ("info/exclude", "info/attributes")}
    try:
        xdg = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "git"
    except RuntimeError:
        xdg = None
    for key, default in (("core.excludesfile", "ignore"), ("core.attributesfile", "attributes")):
        target = Path(os.path.expanduser(configured[key])) if configured.get(key) else None
        if target is None and xdg is not None:
            target = xdg / default
        if target is not None and not target.is_absolute():
            target = root / target
        files[key] = _file_hash(target) if target is not None else None
    return digest({"config": settings, "files": files})


def capture_local_governance(root):
    root = Path(root).resolve()
    visible, governed, _ = _collect_sources(root, _tree(root, git(root, "rev-parse", "HEAD")))
    result = {}
    for file in sorted(governed - visible):
        target = safe_path(root, file)
        if target.exists():
            if not target.is_file():
                raise FactoryError(f"Governance path must be a regular file: {file}")
            result[file] = _governance_digest(target)
    return result


def _snapshot(root, base=None, extra=()):
    """The candidate snapshot plus the internals fingerprint() builds on."""
    config = read_json(root, "factory.json")
    if any(exclusion not in EXCLUDED for exclusion in config.get("evidence_exclude", [])):
        raise FactoryError(
            "Source exclusions cannot hide candidate files; only built-in metadata exclusions are supported"
        )
    head = git(root, "rev-parse", "HEAD")
    if base is not None:
        git(root, "cat-file", "-e", f"{base}^{{commit}}")
    trees = {head: _tree(root, head)}
    if base is not None:
        trees[base] = trees.get(base) or _tree(root, base)
    visible, governed, cached = _collect_sources(root, set().union(*trees.values()))
    governed |= set(extra)
    algorithm = _git_bytes(root, "rev-parse", "--show-object-format").decode().strip()
    if algorithm not in ("sha1", "sha256"):
        raise FactoryError(f"Unsupported Git object format: {algorithm}")
    entries, blobs = _read_files(root, visible | governed, algorithm)
    # A path committed in HEAD but dropped from the index is a change even when
    # its content is untouched; the index no longer tracks it.
    dropped = sorted(set(trees[head]) - cached)
    view = git_view(root)
    candidate = {
        "fingerprint": digest(
            {
                "head": head,
                "entries": entries,
                "runtime": runtime_fingerprint(),
                "index_dropped": dropped,
                "git_view": view,
            }
        ),
        "head": head,
        "git_view": view,
        "source_paths": sorted(visible | governed),
        "dirty_paths": sorted(_content_changes(trees[head], blobs) | set(dropped)),
        "changed_paths": sorted(_content_changes(trees[base or head], blobs) | set(dropped)),
        "entry_hashes": {entry[0]: digest(entry) for entry in entries},
        "unmerged_paths": sorted(_git_paths(root, "diff", "--name-only", "--diff-filter=U", "-z")),
    }
    return candidate, {
        "visible": visible,
        "governed": governed,
        "entries": entries,
        "blobs": blobs,
        "dropped": dropped,
        "baseline": trees[base or head],
    }


def candidate_snapshot(root, base=None):
    return _snapshot(_assert_repository(root), base)[0]


def fingerprint(root, mission, record_root=None):
    root = _assert_repository(root)
    record_root = Path(record_root or root).resolve()
    snapshot = mission.get("governance_snapshot", {})
    candidate, parts = _snapshot(root, mission["base_commit"], snapshot)
    visible, governed, entries = parts["visible"], parts["governed"], parts["entries"]
    hidden_changes = []
    for file in sorted(governed - visible):
        target = safe_path(root, file)
        current = _governance_digest(target) if target.exists() else None
        if current != snapshot.get(file):
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
    visible_blobs = {file: blob for file, blob in parts["blobs"].items() if file in visible}
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
                    "governance_snapshot": snapshot,
                    "task_contracts": contracts,
                    "entries": entries,
                    "spec_hash": spec_hash,
                    "model_plans": plans,
                    "runtime": runtime_fingerprint(),
                    "index_dropped": parts["dropped"],
                    "git_view": candidate["git_view"],
                    **criteria,
                }
            ),
            "criteria_hash": criteria.get("criteria_hash"),
            "changed_paths": sorted(
                _content_changes(parts["baseline"], visible_blobs)
                | set(parts["dropped"])
                | set(hidden_changes)
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


CHANGED_PATHS_SHOWN = 20
IGNORE_PROBE = ".software-factory-ignore-probe"


IGNORED_WRITES_KEPT = 200
# Writes inside the repository's own control files change what Git shows and
# hashes (ignore rules, attributes, filters, hooks); everything else under .git
# (objects, refs, index, logs) is Git bookkeeping and is not a candidate change.
GIT_CONTROL = re.compile(r"\.git/(?:config|info(?:/.*)?|hooks(?:/.*)?)")


def changed_paths_reason(when, paths):
    """A bounded, human-readable list of candidate paths that changed."""
    paths = sorted({printable(p) for p in paths})
    shown = ", ".join(paths[:CHANGED_PATHS_SHOWN])
    more = f" (+{len(paths) - CHANGED_PATHS_SHOWN} more)" if len(paths) > CHANGED_PATHS_SHOWN else ""
    return f"Candidate paths changed {when}: {shown}{more}"


def _inode_state(path):
    """Identity and change markers of a path; any write or relink moves ctime (and mtime on Windows)."""
    try:
        state = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as exc:
        return ("error", exc.errno)
    return (state.st_dev, state.st_ino, state.st_nlink, state.st_ctime_ns, state.st_mtime_ns, state.st_size)


class KnownPaths(set):
    """Candidate paths a monitor treats as source, with the inode state seen when each became known.

    Native events are path based: a write through a hard link elsewhere (for
    example under .git/) reaches the same inode without an event for the
    candidate path, and is only visible in the inode's ctime and link count.
    """

    def __init__(self, root, paths=()):
        super().__init__()
        self.root = Path(root)
        self.states = {}
        self.update(paths)

    def add(self, path):
        self.update((path,))

    def update(self, *groups):
        for group in groups:
            for path in group:
                if path not in self:
                    super().add(path)
                    self.states[path] = _inode_state(self.root / path)

    def __ior__(self, other):
        self.update(other)
        return self


class CandidateMonitor:
    """Observe writes, including write-and-restore, with native OS events.

    Failure to start/drain monitoring fails verification closed. Reading files
    produces open/close events on Linux; only actual mutation events count.

    source_paths are candidate paths; each is also bound to its start inode
    state, compared at close. metadata_prefixes (a historical name) are
    record prefixes the operation must not see written: events at or below
    them count as source changes although they are not candidate paths.
    Git-ignored paths written during monitoring are reported as ignored_writes;
    reads of ignored files are not observable and are not reported.
    """

    def __init__(self, root, source_paths=(), metadata_prefixes=()):
        self.root = Path(root).resolve()
        self.known = KnownPaths(self.root, source_paths)
        self.metadata_prefixes = tuple(metadata_prefixes)
        self.source_changed = False
        self.monitoring_reasons = []
        self._lock = threading.Lock()
        self._pending = set()
        self._moves = []
        self._sequence = 0
        self._suspects = {}
        self._changed = set()
        self._offending = set()
        self._ignored = set()
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
                    files = []
                    for raw in (event.src_path, getattr(event, "dest_path", "")):
                        if not raw:
                            continue
                        try:
                            files.append(Path(raw).relative_to(monitor.root).as_posix())
                        except ValueError:
                            monitor.uncertain("Filesystem monitor observed an out-of-root event")
                    if event.is_directory and event.event_type == "modified":
                        return
                    with monitor._lock:
                        if len(monitor._pending) + len(monitor._moves) >= 65536:
                            monitor.monitoring_reasons.append("Filesystem monitor event bound exceeded")
                            return
                        monitor._sequence += 1
                        moved = event.event_type == "moved" and len(files) == 2
                        if moved:
                            # Remember the pair so a path written inside a directory that
                            # was later moved is judged at the location it ended up in.
                            monitor._moves.append((monitor._sequence, files[0], files[1]))
                        for position, file in enumerate(files):
                            # The source side of a move is not itself a write at that path.
                            written = None if moved and position == 0 else monitor._sequence
                            monitor._pending.add((file, event.is_directory, written))

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
        changed, suspects = self._changed, self._suspects
        known_directories = None
        for file, directory, written in batch:
            if file == ".git" or file.startswith(".git/"):
                if GIT_CONTROL.fullmatch(file):
                    self.source_changed = True
                    changed.add(file)
                continue
            observed = any(file.startswith(p) or p.startswith(file + "/") for p in self.metadata_prefixes)
            if directory and not observed and file not in self.known:
                if known_directories is None:
                    known_directories = {parent.as_posix() for p in self.known for parent in Path(p).parents}
                observed = file in known_directories
            if observed or file in self.known or _governed_event(file):
                self.source_changed = True
                changed.add(file)
            elif not is_mission_record_event(file, directory) and file != ".":
                key = (file, bool(directory))
                previous = suspects.get(key, 0)
                suspects[key] = max(previous if previous is not None else 0, written or 0) or None
        self._classify_suspects()

    @staticmethod
    def _final_location(moves, file, written):
        """Where content written at file ended up after later moves of it or a parent directory.

        moves maps a source path to its (sequence, destination) moves in event
        order; each step follows the earliest move after the previous one.
        """
        since = written or 0
        for _ in range(64):
            parts, step = file.split("/"), None
            for length in range(len(parts), 0, -1):
                source = "/".join(parts[:length])
                later = next(((q, d) for q, d in moves.get(source, ()) if q > since), None)
                if later and (step is None or later[0] < step[0]):
                    step = (later[0], source, later[1])
            if step is None:
                break
            since, source, destination = step
            file = destination + file[len(source) :]
        return file

    def _classify_suspects(self):
        """Judge unknown event paths against the ignore rules in force now.

        Tools such as pytest create a cache directory and only then write its
        own ``.gitignore``; judging each event as it arrives would call the
        cache a candidate change. Known candidate and governance paths are
        never re-judged here: a write-and-restore of them stays a change.
        """
        suspects = self._suspects
        if not suspects:
            self._offending = set()
            return
        moves = {}
        for sequence, source, destination in self._moves:
            moves.setdefault(source, []).append((sequence, destination))
        final = {key: self._final_location(moves, key[0], written) for key, written in suspects.items()}
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

        def git_output(*args, data=None):
            result = subprocess.run(
                ["git", "-C", str(self.root), *args],
                input=data,
                capture_output=True,
                check=False,
                timeout=20,
                env=env,
            )
            if result.returncode not in (0, 1):
                raise FactoryError(result.stderr.decode(errors="replace"))
            return {os.fsdecode(p) for p in result.stdout.split(b"\0") if p}

        try:
            paths = sorted({path + ("/" if key[1] else "") for key, path in final.items()})
            ignored = git_output(
                "check-ignore",
                "--no-index",
                "--stdin",
                "-z",
                data=b"".join(os.fsencode(p) + b"\0" for p in paths),
            )
            # A directory whose ignore rules now ignore any new file in it (for
            # example a cache that wrote its own "*" .gitignore) and that holds
            # no candidate content is not a change by itself; files written
            # inside it are judged on their own events. Other directories still
            # count, since a file created and removed before a watch was added
            # may be visible only through its directory's event.
            candidates = sorted(
                {
                    path
                    for key, path in final.items()
                    if key[1] and path + "/" not in ignored and (self.root / path).is_dir()
                }
            )
            probes = (
                git_output(
                    "check-ignore",
                    "--no-index",
                    "--stdin",
                    "-z",
                    data=b"".join(os.fsencode(f"{d}/{IGNORE_PROBE}") + b"\0" for d in candidates),
                )
                if candidates
                else set()
            )
            directories = [d for d in candidates if f"{d}/{IGNORE_PROBE}" in probes]
            content = (
                git_output(
                    "ls-files",
                    "-z",
                    "--cached",
                    "--others",
                    "--exclude-standard",
                    "--",
                    *(f":(literal){d}" for d in directories),
                )
                if directories
                else set()
            )
            empty = {
                d
                for d in directories
                if not any(f.startswith(d + "/") for f in content)
                and not any(k.startswith(d + "/") for k in self.known)
            }
            self._offending = {
                key[0] if key[0] == path else f"{key[0]} -> {path}"
                for key, path in final.items()
                if (path + "/" if key[1] else path) not in ignored and not (key[1] and path in empty)
            }
            self._ignored = {
                path + "/" if key[1] else path
                for key, path in final.items()
                if (path + "/" if key[1] else path) in ignored
            }
        except (FactoryError, OSError, ValueError, subprocess.SubprocessError) as exc:
            self.uncertain(f"Could not classify filesystem events: {exc}")

    def _check_losses(self):
        losses, start = _event_loss(), self._losses
        if not self._loss_detection:
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
        paths = sorted(self._changed | self._offending)
        reasons = list(dict.fromkeys(self.monitoring_reasons))
        ignored = sorted({printable(p) for p in self._ignored})
        return {
            "source_changed": self.source_changed or bool(paths),
            "monitoring_uncertain": bool(reasons),
            "monitoring_reasons": reasons
            + ([changed_paths_reason("during monitoring", paths)] if paths else []),
            "ignored_writes": ignored[:IGNORED_WRITES_KEPT],
            "ignored_writes_total": len(ignored),
        }

    def _check_inodes(self):
        """Compare each known path's inode state with the state recorded when it became known."""
        linked = []
        for path, before in self.known.states.items():
            after = _inode_state(self.root / path)
            if after != before:
                self.source_changed = True
                self._changed.add(path)
            if any(isinstance(s, tuple) and s[0] != "error" and s[2] > 1 for s in (before, after)):
                linked.append(printable(path))
        if linked:
            shown = ", ".join(sorted(linked)[:CHANGED_PATHS_SHOWN])
            more = (
                f" (+{len(linked) - CHANGED_PATHS_SHOWN} more)" if len(linked) > CHANGED_PATHS_SHOWN else ""
            )
            self.uncertain(
                f"Candidate files have more than one hard link, so writes through another link may be"
                f" unobserved: {shown}{more}"
            )

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
        self._check_inodes()
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


def _scratch_git(scratch, *args, data=None, codes=(0,)):
    """Run Git in a private scratch repository whose objects fall back to the candidate's.

    The scratch repository has no configuration, attributes, filters or
    textconv drivers of its own, and global/system configuration is ignored,
    so the candidate's Git settings cannot change what the diff shows.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(
        {
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_DIR": str(Path(scratch) / "repo.git"),
            "GIT_INDEX_FILE": str(Path(scratch) / "index"),
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_ATTR_NOSYSTEM": "1",
        }
    )
    config = (
        "-c",
        "core.quotePath=true",
        "-c",
        "diff.noprefix=false",
        "-c",
        "diff.mnemonicPrefix=false",
        "-c",
        f"core.attributesFile={os.devnull}",
    )
    try:
        result = subprocess.run(
            ["git", *config, *args],
            cwd=scratch,
            input=data,
            capture_output=True,
            check=False,
            timeout=60,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FactoryError(f"Git diff inspection failed: {exc}") from exc
    if result.returncode not in codes:
        raise FactoryError("Git diff inspection failed: " + result.stderr.decode(errors="replace").strip())
    return result.stdout


def _write_object(objects, data, algorithm):
    """Store data as a loose blob in the scratch object directory; return its id."""
    object_id = _object_id(data, algorithm)
    target = Path(objects) / object_id[:2] / object_id[2:]
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(zlib.compress(b"blob %d\0" % len(data) + data))
    return object_id


def candidate_diff(root, base, paths):
    """Content diff of candidate paths against base: a patch plus per-path line counts.

    Working-tree content is stored unfiltered in a private scratch repository
    (base objects are borrowed through alternates) and compared tree to index,
    so the candidate's clean filters, textconv, diff drivers and "-diff"/binary
    attributes cannot hide or zero a change. Binary detection is Git's content
    heuristic alone. Paths absent from base (untracked or newly added files)
    are rendered as additions. Metadata paths are excluded. "reasons" names
    text files whose repository attributes would hide their diff.
    """
    import tempfile

    root = Path(root).resolve()
    paths = sorted({_require_utf8(p) for p in paths if not is_metadata(p)})
    commit = (
        _git_bytes(root, "rev-parse", "--verify", "--end-of-options", f"{base}^{{commit}}").decode().strip()
    )
    baseline = _tree(root, commit)
    algorithm = _git_bytes(root, "rev-parse", "--show-object-format").decode().strip()
    objects = _git_file(root, "objects").resolve()
    rows, stats, texts, deleted = [], {}, [], []
    with tempfile.TemporaryDirectory(prefix="sf-diff-") as scratch:
        _scratch_git(scratch, "init", "-q", "--bare", f"--object-format={algorithm}", "repo.git")
        store = Path(scratch) / "repo.git/objects"
        (store / "info/alternates").write_text(f"{objects}\n", encoding="utf-8")
        for file in paths:
            target = _candidate_file(root, file)
            if target is None:
                if file in baseline:
                    deleted.append(file)
                    rows.append(b"0 " + b"0" * len(baseline[file][1]) + b"\t" + file.encode() + b"\0")
                continue
            data = target.read_bytes()
            if not _is_binary(data):
                texts.append(file)
            mode = b"100755" if target.stat().st_mode & 0o111 else b"100644"
            rows.append(
                mode + b" " + _write_object(store, data, algorithm).encode() + b"\t" + file.encode() + b"\0"
            )
        _scratch_git(scratch, "read-tree", commit)
        if rows:
            _scratch_git(scratch, "update-index", "-z", "--index-info", data=b"".join(rows))
        patch = _scratch_git(scratch, "diff", "--cached", *_DIFF_OPTIONS, commit)
        for row in _scratch_git(
            scratch,
            "diff",
            "--cached",
            "--numstat",
            "-z",
            "--no-renames",
            "--no-ext-diff",
            "--no-textconv",
            commit,
        ).split(b"\0"):
            if row:
                plus, minus, name = row.split(b"\t", 2)
                binary = plus == b"-"
                stats[_decode_path(name)] = (0 if binary else int(plus), 0 if binary else int(minus), binary)
    reasons = []
    if texts:
        values = _git_bytes(
            root, "check-attr", "-z", "--stdin", "diff", data=b"".join(t.encode() + b"\0" for t in texts)
        )
        fields = values.split(b"\0")
        for index in range(0, len(fields) - 2, 3):
            if fields[index + 2] == b"unset":
                reasons.append(
                    f"Git attributes mark text file {_decode_path(fields[index])} as binary (-diff);"
                    " its diff is still counted as text"
                )
    return {"patch": patch, "stats": stats, "base": base, "deleted": deleted, "reasons": reasons}
