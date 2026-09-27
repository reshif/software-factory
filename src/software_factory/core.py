"""Shared paths, validated records and portable runtime identity."""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import __version__


class FactoryError(Exception):
    def __init__(self, message: str, exit_code: int = 1):
        super().__init__(message)
        self.exit_code = exit_code


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(value if isinstance(value, bytes) else canonical(value)).hexdigest()


def sha256(value: bytes | str) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def assert_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,79}", value):
        raise FactoryError(f"Invalid identifier: {value!r}")
    return value


def safe_path(root: Path | str, relative: str | Path) -> Path:
    root = Path(root).resolve()
    text = str(relative)
    rel = Path(text)
    if (
        not text
        or "\x00" in text
        or "\\" in text
        or rel.is_absolute()
        or (text != "." and any(part in ("", ".", "..") for part in text.split("/")))
        or re.match(r"^[A-Za-z]:", text)
    ):
        raise FactoryError(f"Unsafe relative path: {text}")
    cursor = root
    for part in rel.parts:
        cursor /= part
        if cursor.is_symlink():
            raise FactoryError(f"Symlink in managed path: {text}")
    return cursor


def read_json(root: Path | str, relative: str | Path) -> Any:
    try:
        return json.loads(
            safe_path(root, relative).read_text(encoding="utf-8"),
            parse_constant=lambda x: (_ for _ in ()).throw(ValueError(x)),
        )
    except (OSError, ValueError) as exc:
        raise FactoryError(f"Cannot read JSON {relative}: {exc}") from exc


def write_bytes(root: Path | str, relative: str | Path, data: bytes, mode: int = 0o600) -> None:
    target = safe_path(root, relative)
    target.parent.mkdir(parents=True, exist_ok=True)
    safe_path(root, relative)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        safe_path(root, relative)
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def write_json(root: Path | str, relative: str | Path, value: Any) -> None:
    write_bytes(
        root, relative, (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()
    )


def hash_file(root: Path | str, relative: str | Path) -> str:
    return sha256(safe_path(root, relative).read_bytes())


def asset_root() -> Path:
    return Path(__file__).parent / "data"


def asset_path(root: Path | str, relative: str) -> Path:
    """Prefer reviewed project assets, with package fallback before init."""
    candidate = safe_path(root, f".factory/{relative}")
    if candidate.is_file():
        return candidate
    installation = safe_path(root, ".factory/installation.json")
    if installation.is_file() and not json.loads(installation.read_text()).get("uninstalled", False):
        raise FactoryError(
            f"Missing installed project asset: .factory/{relative}; restore it or perform a reviewed upgrade"
        )
    return asset_root() / relative


def validate(root: Path | str, kind: str, value: Any) -> Any:
    from jsonschema import Draft7Validator

    assert_id(kind)
    try:
        schema = json.loads(asset_path(root, f"schemas/{kind}.schema.json").read_text())
        errors = sorted(Draft7Validator(schema).iter_errors(value), key=lambda e: str(e.path))
    except (OSError, ValueError) as exc:
        raise FactoryError(f"Cannot load {kind} schema: {exc}") from exc
    if errors:
        error = errors[0]
        raise FactoryError(f"Invalid {kind} at {'.'.join(map(str, error.path)) or '<root>'}: {error.message}")
    return value


def profiles(value: Any) -> list[str]:
    if isinstance(value, dict):
        value = value.get("profile")
    if isinstance(value, str):
        value = value.split(",")
    if (
        not isinstance(value, list)
        or not value
        or any(v not in ("claude", "codex", "copilot") for v in value)
        or len(set(value)) != len(value)
    ):
        raise FactoryError("Profiles must be a unique selection of claude,codex,copilot")
    return [p for p in ("claude", "codex", "copilot") if p in value]


def load_config(root: Path | str) -> dict:
    result = validate(root, "factory", read_json(root, "factory.json"))
    profiles(result)
    for key in ("checks", "setup"):
        items = result.get(key, [])
        if len({item["id"] for item in items}) != len(items):
            raise FactoryError(f"Duplicate {key} identifiers")
    if not any(c.get("required") for c in result.get("checks", [])):
        raise FactoryError("At least one required product check must be configured")
    return result


def git(root: Path | str, *args: str, check: bool = True) -> str:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        result = subprocess.run(
            check=False,
            args=["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=20,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise FactoryError(f"Git unavailable: {exc}") from exc
    if check and result.returncode:
        raise FactoryError(f"Git {' '.join(args[:2])} failed: {result.stderr.strip()}")
    return result.stdout.strip("\n")


def resolve_root(value: str | Path | None = None) -> Path:
    candidate = Path(value or Path.cwd()).absolute()
    if not candidate.is_dir():
        raise FactoryError(f"Directory does not exist: {candidate}")
    candidate = candidate.resolve()
    if value is not None:
        return candidate
    try:
        boundary = Path(git(candidate, "rev-parse", "--show-toplevel")).resolve()
    except FactoryError:
        boundary = candidate
    for parent in (candidate, *candidate.parents):
        if (parent / "factory.json").is_file():
            return parent
        if parent == boundary:
            break
    return candidate


CONSTITUTION_PATH = ".factory/CONSTITUTION.md"


def private_dir(root: Path | str, *, create: bool = True) -> Path:
    """Require shared ignore rules; machine-only ignores cannot protect records."""
    root = Path(root)
    path = safe_path(root, ".factory/local")
    if git(root, "ls-files", "--", ".factory/local"):
        raise FactoryError(
            "Private .factory/local records are tracked; remove them from the index deliberately"
        )
    # A non-zero exit (not ignored) yields no output with check=False.
    matched = git(root, "check-ignore", "--no-index", "-v", "--", ".factory/local/", check=False)
    if not matched or ":" not in matched:
        raise FactoryError("Version-controlled ignore rules must exclude .factory/local/")
    source = matched.split(":", 1)[0]
    rule = matched.split("\t", 1)[0].split(":", 2)[-1]
    if (
        source.startswith("/")
        or source == ".git/info/exclude"
        or not source.endswith(".gitignore")
        or rule.startswith("!")
    ):
        raise FactoryError("Private records require a shared .gitignore rule")
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def runtime_fingerprint() -> str:
    """Hash executable Python/assets and actual installed dependency content."""
    files = {}
    base = Path(__file__).parent
    for p in sorted(base.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc":
            files["factory/" + p.relative_to(base).as_posix()] = sha256(p.read_bytes())
    queue = ["jsonschema", "PyYAML", "tomlkit", "watchdog"]
    seen = set()
    while queue:
        name = queue.pop(0)
        normalized = name.lower().replace("_", "-")
        if normalized in seen:
            continue
        seen.add(normalized)
        try:
            dist = importlib.metadata.distribution(name)
        except importlib.metadata.PackageNotFoundError as exc:
            raise FactoryError(
                f"Missing runtime dependency {name}; run uv sync --locked --no-dev --project .factory"
            ) from exc
        for requirement in dist.requires or []:
            if "extra ==" in requirement or "extra==" in requirement:
                continue
            match = re.match(r"[A-Za-z0-9_.-]+", requirement)
            if match:
                try:
                    importlib.metadata.distribution(match.group())
                except importlib.metadata.PackageNotFoundError:
                    continue  # Environment-specific dependency not installed here.
                queue.append(match.group())
        for entry in dist.files or []:
            if entry.suffix == ".pyc" or "__pycache__" in entry.parts or ".." in entry.parts:
                continue
            location = Path(dist.locate_file(entry))
            if not location.is_file():
                raise FactoryError(f"Missing dependency file: {name}/{entry}")
            content = location.read_bytes()
            expected = entry.hash
            if expected and expected.mode == "sha256":
                observed = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
                if observed != expected.value:
                    raise FactoryError(
                        f"Runtime dependency integrity mismatch: {name}/{entry}; restore the locked environment"
                    )
            files[f"{name}/{entry}"] = sha256(content)
    return digest({"version": __version__, "python": sys.version, "files": files})
