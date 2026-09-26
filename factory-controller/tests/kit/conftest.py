"""Shared fixtures/paths for factory-kit (B6) tests."""
from pathlib import Path

import pytest
import yaml

from factory.policy.loader import default_kit_dir

KIT = default_kit_dir()
PLUGIN = KIT / "plugins" / "factory-core"


def parse_frontmatter(path: Path) -> dict:
    """Parse the YAML frontmatter of a `.md` file delimited by `---` lines."""
    text = path.read_text()
    assert text.startswith("---\n"), f"{path} has no YAML frontmatter"
    _, _, rest = text.partition("---\n")
    frontmatter, sep, _body = rest.partition("\n---\n")
    assert sep, f"{path} frontmatter is not terminated with '---'"
    doc = yaml.safe_load(frontmatter)
    assert isinstance(doc, dict), f"{path} frontmatter did not parse to a mapping"
    return doc


@pytest.fixture(scope="session")
def kit_dir() -> Path:
    return KIT


@pytest.fixture(scope="session")
def plugin_dir() -> Path:
    return PLUGIN
