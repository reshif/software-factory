"""factory-kit/guidance/render.py: renders CLAUDE.md and AGENTS.md (spec §3 B6)."""
import importlib.util
import re
import sys

import pytest
import yaml


def _load_render_module(kit_dir):
    path = kit_dir / "guidance" / "render.py"
    spec = importlib.util.spec_from_file_location("factory_kit_render", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def render_module(kit_dir):
    return _load_render_module(kit_dir)


@pytest.fixture
def backend_service_yaml(kit_dir):
    return kit_dir / "templates" / "backend-service" / "factory.yaml"


def test_base_md_has_no_leftover_placeholders_after_render(render_module, kit_dir, backend_service_yaml):
    rendered = render_module.render_product(backend_service_yaml, kit_dir / "guidance" / "base.md")
    assert not re.search(r"\{\{\s*[a-zA-Z0-9_]+\s*\}\}", rendered), "unrendered {{placeholder}} left in output"


def test_render_includes_key_product_fields(render_module, kit_dir, backend_service_yaml):
    rendered = render_module.render_product(backend_service_yaml, kit_dir / "guidance" / "base.md")
    profile = yaml.safe_load(backend_service_yaml.read_text())
    assert profile["product"] in rendered
    assert profile["risk_profile"] in rendered
    assert profile["kit"]["version"] in rendered
    for check in profile["verification"]["required"]:
        assert check in rendered
    for owner in profile["owners"].values():
        assert owner in rendered


def test_render_rejects_unknown_placeholder(render_module):
    with pytest.raises(KeyError):
        render_module.render("{{not_a_real_field}}", {"product": "x"})


def test_main_writes_identical_claude_and_agents_md(render_module, tmp_path, backend_service_yaml):
    exit_code = render_module.main([str(backend_service_yaml), "--out-dir", str(tmp_path)])
    assert exit_code == 0
    claude_md = (tmp_path / "CLAUDE.md").read_text()
    agents_md = (tmp_path / "AGENTS.md").read_text()
    assert claude_md == agents_md
    assert claude_md.strip()


def test_committed_template_guidance_matches_a_fresh_render(render_module, kit_dir, backend_service_yaml):
    """The checked-in templates/backend-service/CLAUDE.md must not have drifted
    from what render.py currently produces from base.md + factory.yaml."""
    fresh = render_module.render_product(backend_service_yaml, kit_dir / "guidance" / "base.md")
    committed = (kit_dir / "templates" / "backend-service" / "CLAUDE.md").read_text()
    assert committed == fresh
    committed_agents = (kit_dir / "templates" / "backend-service" / "AGENTS.md").read_text()
    assert committed_agents == fresh


def test_render_script_has_an_executable_mode(kit_dir):
    """Shebang/mode mismatch fix: a `#!/usr/bin/env python3` script that isn't
    executable can't actually be run the way its shebang promises."""
    render_py = kit_dir / "guidance" / "render.py"
    assert render_py.read_text().splitlines()[0] == "#!/usr/bin/env python3"
    assert render_py.stat().st_mode & 0o111, "render.py has a shebang but is not executable"


def test_render_product_validates_against_factory_schema(render_module, kit_dir, tmp_path):
    """R-A7/low-priority fix: render.py must validate factory.yaml against
    factory.schema.json before rendering, not just duck-type its way through
    build_context() and produce guidance from a document the rest of the
    factory would reject outright."""
    bad_yaml = tmp_path / "factory.yaml"
    bad_yaml.write_text("schema_version: 1\nproduct: x\nrisk_profile: yolo\n")

    with pytest.raises(render_module.FactoryYamlError, match="risk_profile"):
        render_module.render_product(bad_yaml, kit_dir / "guidance" / "base.md")


def test_main_reports_an_invalid_factory_yaml_without_writing_output(render_module, kit_dir, tmp_path):
    bad_yaml = tmp_path / "factory.yaml"
    bad_yaml.write_text("schema_version: 1\nproduct: x\nrisk_profile: yolo\n")

    exit_code = render_module.main([str(bad_yaml), "--out-dir", str(tmp_path / "out")])
    assert exit_code != 0
    assert not (tmp_path / "out" / "CLAUDE.md").exists()


def test_valid_factory_yaml_still_renders_after_adding_validation(render_module, kit_dir, backend_service_yaml):
    """Guard against the validation step itself becoming too strict for the
    kit's own examples (the schema gained a `checks` object form after this
    template was first written)."""
    rendered = render_module.render_product(backend_service_yaml, kit_dir / "guidance" / "base.md")
    assert rendered.strip()
