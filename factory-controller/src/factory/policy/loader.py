"""Load and validate the factory-kit policies."""
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .requirements import Requirement, parse_requirement

GATES = ("h1", "hm", "h2")
PROFILES = ("experimental", "standard", "regulated")
CLASSES = tuple(f"AC{i}" for i in range(1, 9))
LEVELS = ("L3", "L4", "L5")


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Relaxation:
    gate: str
    classes: frozenset
    profiles: frozenset
    to: Requirement
    sample_rate: float | None = None


@dataclass(frozen=True)
class Floor:
    forbidden_paths: tuple
    protected_paths: tuple
    migration_paths: tuple
    test_globs: tuple
    weakening_patterns: tuple
    dependency_files: tuple
    docs_globs: tuple
    small_fix_max_lines: int
    existing_tests_protected: bool = True


@dataclass(frozen=True)
class Policy:
    policy_version: str
    base: dict                      # class -> gate -> profile -> Requirement
    hx_quorum: dict                 # profile -> Requirement
    relaxations: dict               # level -> tuple[Relaxation]
    never_relax: frozenset
    max_level_by_profile: dict
    floor: Floor
    source: Path = field(default=None, compare=False)


def default_kit_dir() -> Path:
    env = os.environ.get("FACTORY_KIT_DIR")
    if env:
        return Path(env)
    # src/factory/policy/loader.py -> repo root is parents[4]
    return Path(__file__).resolve().parents[4] / "factory-kit"


def _read(path: Path) -> dict:
    with open(path) as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise PolicyError(f"{path}: missing or unsupported schema_version")
    return data


def load_policy(kit_dir: Path | None = None) -> Policy:
    kit_dir = Path(kit_dir) if kit_dir else default_kit_dir()
    policies = kit_dir / "policies"
    gate_table = _read(policies / "gate-table.yaml")
    autonomy = _read(policies / "autonomy-levels.yaml")
    floor_doc = _read(policies / "floor.yaml")

    versions = {gate_table.get("policy_version"), autonomy.get("policy_version"),
                floor_doc.get("policy_version")}
    if len(versions) != 1 or None in versions:
        raise PolicyError(f"policy_version mismatch across policy files: {sorted(map(str, versions))}")

    base = {}
    for cls in CLASSES:
        row = gate_table["base"].get(cls)
        if row is None:
            raise PolicyError(f"gate table missing class {cls}")
        base[cls] = {}
        for gate in GATES:
            cells = row.get(gate) or {}
            missing = set(PROFILES) - set(cells)
            if missing:
                raise PolicyError(f"gate table {cls}.{gate} missing profiles {sorted(missing)}")
            base[cls][gate] = {p: parse_requirement(cells[p]) for p in PROFILES}
    for cls in CLASSES:
        if any(r.kind == "standing" for r in base[cls]["hm"].values()):
            raise PolicyError(f"{cls}.hm cannot be 'standing'")

    hx_quorum = {p: parse_requirement(v) for p, v in gate_table["hx_quorum"].items()}

    never_relax = frozenset(autonomy.get("never_relax", []))
    levels_doc = autonomy["levels"]
    relaxations = {}
    for level in LEVELS:
        entry = levels_doc.get(level)
        if entry is None:
            raise PolicyError(f"autonomy levels missing {level}")
        items = list(relaxations.get(entry["inherits"], ())) if entry.get("inherits") else []
        for r in entry.get("relaxations") or []:
            relax = Relaxation(gate=r["gate"], classes=frozenset(r["classes"]),
                               profiles=frozenset(r["profiles"]), to=parse_requirement(r["to"]),
                               sample_rate=r.get("sample_rate"))
            if relax.gate not in GATES:
                raise PolicyError(f"{level}: unknown gate {relax.gate}")
            forbidden = relax.classes & never_relax
            if forbidden:
                raise PolicyError(f"{level}: relaxation names never-relax classes {sorted(forbidden)}")
            items.append(relax)
        relaxations[level] = tuple(items)

    floor = Floor(
        forbidden_paths=tuple(floor_doc["forbidden_paths"]),
        protected_paths=tuple(floor_doc["protected_paths"]),
        migration_paths=tuple(floor_doc.get("migration_paths", ["migrations/**", "**/migrations/**"])),
        test_globs=tuple(floor_doc["test_globs"]),
        weakening_patterns=tuple(floor_doc["weakening_patterns"]),
        dependency_files=tuple(floor_doc["dependency_files"]),
        docs_globs=tuple(floor_doc["docs_globs"]),
        small_fix_max_lines=int(floor_doc["small_fix_max_lines"]),
        existing_tests_protected=bool(floor_doc.get("existing_tests_protected", True)),
    )

    return Policy(policy_version=versions.pop(), base=base, hx_quorum=hx_quorum,
                  relaxations=relaxations, never_relax=never_relax,
                  max_level_by_profile=dict(autonomy["max_level_by_profile"]),
                  floor=floor, source=kit_dir)
