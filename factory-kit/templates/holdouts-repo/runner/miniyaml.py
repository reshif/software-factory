"""A tiny, hand-rolled subset of YAML: nested mappings and scalars only.

`run_blackbox.py` must stay stdlib-only (no PyYAML), so scenario files use
just enough YAML for a plain reader to parse by hand: `key: value` lines,
nesting expressed by indentation, `#` comments, and scalars coerced to
int/float/bool where they look like one. **No lists, anchors, multiline
strings or flow style.** Every real YAML parser reads these files the same
way a human would, since they're a strict, ordinary subset -- this module
just avoids taking a dependency to do it.
"""
from __future__ import annotations


def _coerce(value: str):
    if value == "":
        return ""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    if value.lower() in ("true", "false"):
        return value.lower() == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        pass
    return value


def parse(text: str) -> dict:
    """Parse a scenario file into a nested dict of str -> (scalar | dict)."""
    rows: list[tuple[int, str, object]] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if ":" not in stripped:
            raise ValueError(f"expected 'key: value', got: {raw!r}")
        key, _, value = stripped.partition(":")
        rows.append((indent, key.strip(), _coerce(value.strip())))

    if not rows:
        return {}

    pos = 0

    def parse_block(indent: int) -> dict:
        nonlocal pos
        block: dict = {}
        while pos < len(rows) and rows[pos][0] == indent:
            _, key, value = rows[pos]
            pos += 1
            if value == "" and pos < len(rows) and rows[pos][0] > indent:
                value = parse_block(rows[pos][0])
            block[key] = value
        return block

    return parse_block(rows[0][0])
