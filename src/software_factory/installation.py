"""Curated, versioned project installation and conservative upgrades."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__
from .core import (
    FactoryError,
    asset_root,
    git,
    load_config,
    profiles,
    read_json,
    safe_path,
    sha256,
    validate,
)
from .rendering import SHARED, plan_render, read_manifest, strip_owned, unmark_section
from .transactions import apply, ensure_no_journal, optional, planned_preimages, planning_snapshot

MANIFEST = ".factory/installation.json"


def starter(name: str, selected) -> dict:
    selected = profiles(selected)
    return {
        "schema_version": 1,
        "name": name,
        "profile": selected[0] if len(selected) == 1 else selected,
        "completion_target": "READY_PR",
        "work_types": ["feature", "patch", "maintenance"],
        "limits": {"repair_attempts": 3, "parallel_writers": 1, "check_timeout_seconds": 300},
        "checks": [
            {
                "id": "configure-me",
                "command": [
                    "python",
                    "-c",
                    "raise SystemExit('Configure real product checks in factory.json')",
                ],
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
        "evidence_exclude": [".factory/missions/", ".factory/local/", "factory.lock.json"],
        "model_selection": {"mode": "recommend"},
        "jev": {
            "enabled": False,
            "provider": "typesafe",
            "model": "jev-1.13.0",
            "claim_mode": "shadow",
        },
    }


def kernel_payload() -> dict[str, bytes]:
    data = asset_root()
    files = {}
    allowed_dirs = {"schemas", "roles", "skills", "prompts", "vendors", "models", "templates", "docs"}
    allowed_files = {"CONSTITUTION.md", "registry.json", "policy.json", "workflow.json"}
    for p in sorted(data.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(data)
        if p.is_symlink():
            raise FactoryError("Symlink in package assets")
        if rel.parts[0] in allowed_dirs or rel.as_posix() in allowed_files:
            files[".factory/" + rel.as_posix()] = p.read_bytes()
    package = Path(__file__).parent
    for p in sorted(package.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
            if p.is_symlink():
                raise FactoryError("Symlink in package runtime")
            files[".factory/src/software_factory/" + p.relative_to(package).as_posix()] = p.read_bytes()
    for name in ("pyproject.toml", "uv.lock"):
        template = data / "runtime" / name
        if not template.is_file():
            raise FactoryError(f"Incomplete release: missing runtime/{name}")
        files[".factory/" + name] = template.read_bytes()
    files[".factory/README.md"] = (
        b"# Project factory runtime\n\nRun uv sync --locked --no-dev --project .factory to restore this exact runtime.\n"
    )
    files[".factory/run.py"] = (
        b'import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).resolve().parent / "src"))\nfrom software_factory.cli import main\nmain()\n'
    )
    return files


def _target(root: Path, allow_dirty: bool, *, allow_no_git=True):
    if root.is_symlink():
        raise FactoryError("Target must not be a symlink")
    if root.exists() and not root.is_dir():
        raise FactoryError("Target is not a directory")
    if shutil.which("git") is None:
        # Ignore-rule validation and baselines need Git even for a plain folder.
        raise FactoryError("git is required to install the software factory; install Git and retry")
    if not root.is_dir():
        return False
    top = git(root, "rev-parse", "--show-toplevel", check=False)
    if not top:
        if not allow_no_git:
            raise FactoryError("This operation requires an existing Git working tree")
        return False
    top = Path(top).resolve()
    if top != root.resolve():
        raise FactoryError(f"Target is inside a repository; explicitly select its root: {top}")
    if not allow_dirty and git(root, "status", "--porcelain"):
        raise FactoryError(
            "Target has uncommitted changes; inspect them and use --allow-dirty if intentional"
        )
    return True


def ignore_plan(root: Path) -> bytes:
    existing = optional(root, ".gitignore") or b""
    text = existing.decode()
    needed = [".factory/.venv/", ".factory/local/", ".factory-install.lock", "__pycache__/"]
    if root.exists():
        try:
            if git(root, "ls-files", "--", ".factory/local", ".factory/.venv"):
                raise FactoryError(
                    "Private factory runtime/log files are tracked; resolve before installation"
                )
        except FactoryError as exc:
            if "tracked" in str(exc):
                raise
    # Appending after negations ensures the root rule wins; a nested conflicting
    # rule is independently tested in an isolated Git tree below.
    marker = "# software-factory private/runtime files"
    block = marker + "\n" + "\n".join(needed) + "\n"
    # Idempotent: user rules may follow the block; its presence is sufficient
    # because the shadow check below still proves the effective result.
    if block not in text:
        text += ("\n" if text and not text.endswith("\n") else "") + block
    with tempfile.TemporaryDirectory(prefix="sf-ignore-") as temporary:
        shadow = Path(temporary)
        git(shadow, "init", "-q")
        (shadow / ".gitignore").write_text(text)
        nested = optional(root, ".factory/.gitignore")
        (shadow / ".factory/local").mkdir(parents=True)
        (shadow / ".factory/.venv").mkdir()
        if nested is not None:
            (shadow / ".factory/.gitignore").write_bytes(nested)
        for name in (".factory/local/", ".factory/.venv/"):
            env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
            env.update({"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"})
            try:
                result = subprocess.run(
                    check=False,
                    timeout=20,
                    args=[
                        "git",
                        "-c",
                        "core.excludesFile=" + os.devnull,
                        "-C",
                        str(shadow),
                        "check-ignore",
                        "--no-index",
                        "-q",
                        "--",
                        name,
                    ],
                    env=env,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise FactoryError(f"Git unavailable: {exc}") from exc
            if result.returncode:
                raise FactoryError(f"Nested ignore rules expose {name}; resolve before installation")
    return text.encode()


def _validate_manifest(manifest) -> None:
    """Validate with this release's schema, not a project copy that may be customized."""
    from jsonschema import Draft7Validator

    schema = json.loads((asset_root() / "schemas/installation.schema.json").read_text())
    errors = sorted(Draft7Validator(schema).iter_errors(manifest), key=lambda e: str(e.path))
    if errors:
        location = ".".join(map(str, errors[0].path)) or "<root>"
        raise FactoryError(
            f"Invalid installation manifest at {location}: {errors[0].message}; no changes applied"
        )


def load_installation(root: Path) -> dict | None:
    if optional(root, MANIFEST) is None:
        return None
    manifest = read_json(root, MANIFEST)
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 2
        or manifest.get("runtime") != "python-uv"
        or not isinstance(manifest.get("files"), dict)
    ):
        raise FactoryError("Unsupported installation manifest; no changes applied")
    _validate_manifest(manifest)
    for name, record in manifest["files"].items():
        if (
            not name.startswith(".factory/")
            or name.startswith((".factory/local/", ".factory/missions/", ".factory/.venv/"))
            or name == MANIFEST
            or not isinstance(record.get("managed"), bool)
        ):
            raise FactoryError(f"Invalid installation ownership: {name}")
        safe_path(root, name)
    return manifest


def _sync(path: Path, *, offline=False):
    executable = shutil.which("uv")
    if not executable:
        raise FactoryError("uv is required for runtime setup; install uv or use --skip-sync")
    command = [executable, "sync", "--locked", "--no-dev", "--project", str(path), "--python", sys.executable]
    if offline:
        command.append("--offline")
    result = subprocess.run(check=False, args=command, capture_output=True, timeout=180)
    if result.returncode:
        # Package-manager output may contain private index URLs; keep it out of
        # records and explain the exact recovery command instead.
        raise FactoryError(
            "uv runtime setup failed; inspect uv sync --locked --no-dev --project .factory locally", 2
        )


def release_key(version: str) -> tuple[int, ...] | None:
    match = re.match(r"(\d+(?:\.\d+)*)", version or "")
    if not match:
        return None
    parts = [int(n) for n in match[1].split(".")]
    while len(parts) > 1 and parts[-1] == 0:
        parts.pop()
    return tuple(parts)


# Records that persist in project history, by the schema (and definition) that validates them.
HISTORY_RECORDS = {
    ".factory/schemas/mission.schema.json": ((".factory/missions/*/mission.json",), None),
    ".factory/schemas/evidence.schema.json": ((".factory/missions/*/evidence/**/checks.json",), None),
    ".factory/schemas/result.schema.json": ((".factory/missions/*/results/records/*.json",), None),
    ".factory/schemas/result-index.schema.json": ((".factory/missions/*/results/index.json",), None),
    ".factory/schemas/models.schema.json": ((".factory/missions/*/models/*.json",), "plan"),
    ".factory/schemas/factory.schema.json": (("factory.json",), None),
}
FACTORY_SCHEMA = ".factory/schemas/factory.schema.json"
# The installed copy predates this release; name this release's runbook section.
TRANSITION_DOC = "'Schema changes and mission history' in this release's docs/runbooks/upgrading.md"


def validate_history(root: Path, schema_changes, effective) -> dict:
    """Validate every persisted record against the upgraded schemas, or refuse the upgrade.

    Records are committed history: finishing or canceling missions with the old
    release does not remove them, so each must satisfy the new release's schema.
    """
    from jsonschema import Draft7Validator

    unmapped = [n for n in schema_changes if n not in HISTORY_RECORDS or effective.get(n) is None]
    if unmapped:
        raise FactoryError(
            "Historical mission schemas would change without a known record validation; "
            f"a reviewed data transition is required (see {TRANSITION_DOC}): " + ", ".join(unmapped)
        )
    missions = safe_path(root, ".factory/missions")
    if missions.is_dir():
        # Glob silently skips (or follows) symlinked directories depending on the
        # Python version; refuse them so no record escapes or evades validation.
        for current, dirs, names in os.walk(missions, followlinks=False):
            for name in dirs + names:
                safe_path(root, (Path(current) / name).relative_to(root).as_posix())
    validated, failures = [], []
    for name in sorted(schema_changes):
        patterns, definition = HISTORY_RECORDS[name]
        schema = json.loads(effective[name])
        if definition:
            schema.pop("oneOf", None)
            schema["$ref"] = f"#/definitions/{definition}"
        validator = Draft7Validator(schema)
        files = sorted({p.relative_to(root).as_posix() for pattern in patterns for p in root.glob(pattern)})
        for file in files:
            try:
                value = json.loads(safe_path(root, file).read_bytes())
                error = next(iter(sorted(validator.iter_errors(value), key=lambda e: str(e.path))), None)
                reason = error and f"{'.'.join(map(str, error.path)) or '<root>'}: {error.message}"
            except (OSError, ValueError) as exc:
                reason = f"unreadable: {exc}"
            if reason:
                failures.append(f"{file} ({reason})")
            else:
                validated.append(file)
    if failures:
        shown = failures[:10] + ([f"and {len(failures) - 10} more"] if len(failures) > 10 else [])
        config = any(f.startswith("factory.json (") for f in failures)
        raise FactoryError(
            "Historical records do not satisfy this release's schemas; no changes applied. "
            + ("Update factory.json to satisfy this release's factory schema. " if config else "")
            + "Fix the records or archive the mission outside .factory/missions in a reviewed commit "
            f"(see {TRANSITION_DOC}): " + "; ".join(shown)
        )
    return {"schemas": sorted(schema_changes), "validated_records": validated}


def install(
    root: Path,
    *,
    selected=None,
    dry_run=False,
    allow_dirty=False,
    git_init=False,
    skip_sync=False,
    upgrade=False,
    to_version=None,
    allow_downgrade=False,
) -> dict:
    root = Path(root).absolute()
    has_git = _target(root, allow_dirty)
    if root.is_dir():
        ensure_no_journal(root)
    snapshot = planning_snapshot(root)
    if to_version and to_version != __version__:
        raise FactoryError(
            f"This artifact is {__version__}; run upgrade using the desired version's wheel/uv tool environment"
        )
    prior = load_installation(root)
    if upgrade and prior is None:
        raise FactoryError("No installation baseline; use init")
    if upgrade and not allow_downgrade:
        installed, current = release_key(prior["version"]), release_key(__version__)
        if installed is not None and current is not None and installed > current:
            raise FactoryError(
                f"Installed factory {prior['version']} is newer than this release {__version__}; "
                "upgrade with the newer release, or pass --allow-downgrade to downgrade deliberately"
            )
    payload = kernel_payload()
    if (
        prior
        and not upgrade
        and (
            prior["version"] != __version__
            or prior.get("payload_sha256")
            != sha256(json.dumps({n: sha256(b) for n, b in payload.items()}, sort_keys=True))
        )
    ):
        raise FactoryError("A different release is installed; use upgrade")
    if optional(root, "factory.json") is not None:
        config = load_config(root)
        if selected is not None and profiles(config) != profiles(selected):
            raise FactoryError("Installed profiles differ; use render --profile explicitly")
    else:
        if selected is None:
            raise FactoryError("Select --profile claude, codex, copilot, or a comma-separated combination")
        config = starter(root.name, selected)
        validate(root, "factory", config)
    operations = {}
    effective = {}
    records = {}
    conflicts = []
    existing_files = prior["files"] if prior else {}
    for name in sorted(set(payload) | set(existing_files)):
        upstream, local, baseline = payload.get(name), optional(root, name), existing_files.get(name)
        upstream_hash = sha256(upstream) if upstream is not None else None
        local_hash = sha256(local) if local is not None else None
        managed = baseline.get("managed", False) if baseline else local is None
        if baseline and not managed:
            if local is not None:
                effective[name] = local
        elif baseline:
            if prior.get("uninstalled") and local is None:
                operations[name] = upstream
            elif local_hash == upstream_hash:
                pass
            elif upstream_hash == baseline["sha256"]:
                pass  # Preserve deliberate local changes/deletions.
            elif local_hash == baseline["sha256"]:
                operations[name] = upstream
            else:
                conflicts.append(name)
        elif local is None:
            operations[name] = upstream
        else:
            conflicts.append(name)
        final = operations.get(name, local)
        if final is not None:
            effective[name] = final
        if upstream is not None:
            records[name] = {"sha256": upstream_hash, "managed": managed}
    if conflicts:
        raise FactoryError("Installation conflicts; no changes applied: " + ", ".join(conflicts))
    history = None
    if upgrade:
        missions = safe_path(root, ".factory/missions")
        has_missions = missions.is_dir() and any(missions.iterdir())
        # factory.json is always present, so a factory schema change is validated even
        # without missions; schemas new to this release are validated when records use them.
        schema_changes = [
            n
            for n in operations
            if n.startswith(".factory/schemas/")
            and n != ".factory/schemas/installation.schema.json"
            and (has_missions or n == FACTORY_SCHEMA)
            and (n in HISTORY_RECORDS or optional(root, n) is not None)
        ]
        if schema_changes:
            history = validate_history(root, schema_changes, effective)
    # Required runtime assets may not be deleted even when preserving a local change.
    for name in (
        ".factory/CONSTITUTION.md",
        ".factory/registry.json",
        ".factory/pyproject.toml",
        ".factory/uv.lock",
    ):
        if name not in effective:
            raise FactoryError(f"Required runtime asset missing: {name}")
    # A reinstall after uninstall is a fresh install: recreate absent exports,
    # while exports kept (edited) at uninstall stay user-owned.
    render_changes, render_report = plan_render(
        root, config=config, payload=effective, restore=bool(prior and prior.get("uninstalled"))
    )
    operations.update(render_changes)
    operations[".gitignore"] = ignore_plan(root)
    if optional(root, "factory.json") is None:
        operations["factory.json"] = (json.dumps(config, indent=2) + "\n").encode()
    manifest = {
        "schema_version": 2,
        "runtime": "python-uv",
        "version": __version__,
        "payload_sha256": sha256(json.dumps({n: sha256(b) for n, b in payload.items()}, sort_keys=True)),
        "files": records,
    }
    operations[MANIFEST] = (json.dumps(manifest, indent=2) + "\n").encode()
    expected = planned_preimages(root, snapshot, operations)
    changed = [name for name, value in operations.items() if expected[name] != value]
    report = {
        "version": __version__,
        "target": str(root),
        "dry_run": dry_run,
        "changed": sorted(changed),
        "profiles": render_report["profiles"],
        "prompt_commands": render_report["prompt_commands"],
        "jev": jev_summary(config),
        "relinquished_exports": render_report["relinquished"],
        "network": "none" if skip_sync or dry_run else "uv dependency setup only",
        "setup": "planned",
    }
    if history is not None:
        report["history"] = history
    if dry_run:
        return report
    # Warm and validate the complete runtime before any target write.
    if not skip_sync:
        with tempfile.TemporaryDirectory(prefix="sf-runtime-stage-") as temporary:
            stage = Path(temporary)
            for name, data in effective.items():
                rel = name.removeprefix(".factory/")
                target = stage / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
            _sync(stage)
    planned_preimages(root, snapshot, operations)
    root.mkdir(parents=True, exist_ok=True)
    if git_init and not has_git:
        git(root, "init", "-q")
        has_git = True
    apply(root, operations, expected=expected, label="upgrade" if upgrade else "init")
    if not skip_sync:
        try:
            _sync(root / ".factory", offline=True)
        except (FactoryError, subprocess.TimeoutExpired):
            # doctor reports runtime_missing until the recovery command succeeds.
            raise FactoryError(
                "Project files installed; runtime setup incomplete. Run uv sync --locked --no-dev --project .factory, then doctor",
                2,
            )
    report["setup"] = "dependencies_missing" if skip_sync else "configured"
    report["baseline"] = (
        "present"
        if has_git and git(root, "rev-parse", "--verify", "HEAD", check=False)
        else "required_before_missions"
    )
    report["checks"] = (
        "configuration_required" if any(c["id"] == "configure-me" for c in config["checks"]) else "not_run"
    )
    report["live_client_behavior"] = "not_run"
    return report


def jev_summary(config: dict) -> dict:
    """jev.enabled selects the model selector and enables claim assessment."""
    settings = config.get("jev", {})
    enabled = bool(settings.get("enabled"))
    return {
        "enabled": enabled,
        "model_selection": "jev" if enabled else "factory-models",
        "claim_mode": settings.get("claim_mode", "shadow") if enabled else "off",
    }


def uninstall(root: Path, *, dry_run=False) -> dict:
    root = Path(root)
    ensure_no_journal(root)
    snapshot = planning_snapshot(root)
    prior = load_installation(root)
    if prior is None:
        raise FactoryError("No Python factory installation exists")
    operations = {}
    preserved = []
    unmarked = []
    for name, record in prior["files"].items():
        current = optional(root, name)
        if record["managed"] and current is not None and sha256(current) == record["sha256"]:
            operations[name] = None
        elif current is not None:
            preserved.append(name)
    rendered = read_manifest(root)
    # Preserved (edited) exports become user-owned: their ownership records are
    # dropped so a later init neither deadlocks nor overwrites them. Whole files
    # stay listed as relinquished so init skips them instead of colliding. An
    # edited shared section keeps its text byte for byte but loses its two marker
    # lines, so it becomes ordinary user text and a later init can add a fresh one.
    relinquished = {n for n in (rendered or {}).get("relinquished", []) if optional(root, n) is not None}
    for name, record in (rendered or {}).get("generated", {}).items():
        if optional(root, name) is None:
            continue  # Already removed by the user; nothing is left to detach.
        try:
            operations[name] = strip_owned(root, name, record)
        except FactoryError:
            preserved.append(name)
            if record["kind"] == "block":
                try:
                    operations[name] = unmark_section(root, name, record)
                    unmarked.append(name)
                except FactoryError:
                    pass  # Missing or duplicated markers: left for the user to resolve.
            if name not in SHARED and record["kind"] == "file":
                relinquished.add(name)
    if rendered and not relinquished:
        operations["factory.lock.json"] = None
    elif rendered:
        rendered["generated"] = {}
        rendered["relinquished"] = sorted(relinquished)
        operations["factory.lock.json"] = (json.dumps(rendered, indent=2) + "\n").encode()
    # Preserve provenance for deliberate reinstall and historical records.
    archive = dict(prior)
    archive["uninstalled"] = True
    archive["preserved"] = sorted(preserved)
    operations[MANIFEST] = (json.dumps(archive, indent=2) + "\n").encode()
    expected = planned_preimages(root, snapshot, operations)
    if not dry_run:
        apply(root, operations, expected=expected, label="uninstall")
    return {
        "dry_run": dry_run,
        "removed_or_detached": sorted(n for n in operations if n != MANIFEST),
        "preserved": sorted(preserved),
        "unmarked_sections": sorted(unmarked),
        "retained": [
            "factory.json",
            ".factory/missions",
            ".factory/local",
            ".factory/.venv",
            ".gitignore",
            MANIFEST,
        ],
    }
