"""Path glob matching with `**` support.

`*` matches within one path segment, `?` one character, `**/` zero or more
directories, and a trailing `**` everything below.
"""
import re
from functools import lru_cache
from typing import Iterable


@lru_cache(maxsize=2048)
def _compile(pattern: str) -> re.Pattern:
    out = []
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if c == "*":
            if pattern.startswith("**/", i):
                out.append("(?:.*/)?")
                i += 3
                continue
            if pattern.startswith("**", i):
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z")


def match(path: str, pattern: str) -> bool:
    return _compile(pattern).match(path) is not None


def match_any(path: str, patterns: Iterable[str]) -> bool:
    return any(match(path, p) for p in patterns)


def matching(paths: Iterable[str], patterns: Iterable[str]) -> list[str]:
    patterns = tuple(patterns)
    return [p for p in paths if match_any(p, patterns)]
