"""`pipeline.products.ProductRegistry` (build spec §3 B7)."""
import shutil

import pytest
import yaml

from factory.pipeline.products import ProductConfigError, ProductRegistry
from factory.policy.loader import default_kit_dir

KIT_DIR = default_kit_dir()


def _patch_verification_floor(factory_yaml, extra_required=()):
    """The template's verification.required is [lint, unit]; the verification
    floor (red team #3 item 15) additionally requires review_agent always, and
    holdout_blackbox for standard/regulated products (this template is
    standard) -- patch it in so tests aren't all about the floor by accident."""
    doc = yaml.safe_load(factory_yaml.read_text())
    required = list(doc["verification"]["required"])
    for name in ("review_agent", "holdout_blackbox", *extra_required):
        if name not in required:
            required.append(name)
    doc["verification"]["required"] = required
    factory_yaml.write_text(yaml.safe_dump(doc))
    return doc


@pytest.fixture
def products_dir(tmp_path):
    root = tmp_path / "products"
    root.mkdir()
    shutil.copytree(KIT_DIR / "templates" / "backend-service", root / "backend-service")
    _patch_verification_floor(root / "backend-service" / "factory.yaml")
    return root


def test_loads_the_golden_path_template(products_dir):
    registry = ProductRegistry(products_dir, KIT_DIR)
    product = registry.get("backend-service")
    assert product.repo == "org/backend-service"
    assert product.risk_profile == "standard"
    assert product.checks["lint"]["argv"] == ["python", "-m", "compileall", "-q", "app", "tests"]
    assert len(product.standing_mandates) == 1
    assert product.standing_mandates[0].mandate_id == "SM-patch"


def test_verification_floor_requires_review_agent(tmp_path):
    root = tmp_path / "products"
    root.mkdir()
    product_dir = root / "backend-service"
    shutil.copytree(KIT_DIR / "templates" / "backend-service", product_dir)
    # Only patch in holdout_blackbox, deliberately leaving review_agent out.
    factory_yaml = product_dir / "factory.yaml"
    doc = yaml.safe_load(factory_yaml.read_text())
    doc["verification"]["required"] = ["lint", "unit", "holdout_blackbox"]
    factory_yaml.write_text(yaml.safe_dump(doc))
    with pytest.raises(ProductConfigError, match="review_agent"):
        ProductRegistry(root, KIT_DIR)


def test_verification_floor_requires_holdout_for_standard_and_regulated(tmp_path):
    root = tmp_path / "products"
    root.mkdir()
    product_dir = root / "backend-service"
    shutil.copytree(KIT_DIR / "templates" / "backend-service", product_dir)
    factory_yaml = product_dir / "factory.yaml"
    doc = yaml.safe_load(factory_yaml.read_text())
    doc["verification"]["required"] = ["lint", "unit", "review_agent"]  # no holdout_blackbox
    factory_yaml.write_text(yaml.safe_dump(doc))
    with pytest.raises(ProductConfigError, match="holdout_blackbox"):
        ProductRegistry(root, KIT_DIR)

    # An experimental product, on the other hand, doesn't need it.
    doc["risk_profile"] = "experimental"
    doc["autonomy_level"] = "L3"
    factory_yaml.write_text(yaml.safe_dump(doc))
    registry = ProductRegistry(root, KIT_DIR)
    assert "holdout_blackbox" not in registry.get("backend-service").verification_required


def test_repo_maps_back_to_the_product(products_dir):
    registry = ProductRegistry(products_dir, KIT_DIR)
    assert registry.for_repo("org/backend-service").name == "backend-service"


def test_for_repo_unregistered_raises(products_dir):
    registry = ProductRegistry(products_dir, KIT_DIR)
    with pytest.raises(ProductConfigError):
        registry.for_repo("someone/else")


def test_approvers_for_codeowners_hm_resolves_to_tech_lead_and_security(products_dir):
    # The template has no `security` owner, so codeowners resolves to tech_lead alone.
    registry = ProductRegistry(products_dir, KIT_DIR)
    product = registry.get("backend-service")
    assert set(product.approvers_for("HM")) == {"@tl"}
    assert set(product.approvers_for("hm")) == {"@tl"}
    assert "@po" not in product.approvers_for("HM"), "codeowners is tech_lead/security, not product"


def test_eligible_for_codeowners_includes_a_security_owner_when_present(products_dir):
    factory_yaml = products_dir / "backend-service" / "factory.yaml"
    doc = yaml.safe_load(factory_yaml.read_text())
    doc["owners"]["security"] = "@sec"
    factory_yaml.write_text(yaml.safe_dump(doc))
    registry = ProductRegistry(products_dir, KIT_DIR)
    product = registry.get("backend-service")
    eligible = product.eligible_for("hm")
    assert set(eligible) == {"@tl", "@sec"}
    assert eligible["@sec"] == ("security",)


def test_eligible_for_a_plain_gate_uses_the_configured_roles(products_dir):
    registry = ProductRegistry(products_dir, KIT_DIR)
    product = registry.get("backend-service")
    eligible = product.eligible_for("H1")
    assert eligible == {"@po": ("product",), "@tl": ("tech_lead",)}


def test_owner_roles_invert_the_owners_map(products_dir):
    registry = ProductRegistry(products_dir, KIT_DIR)
    product = registry.get("backend-service")
    assert product.roles_for("@po") == ("product",)
    assert product.roles_for("@tl") == ("tech_lead",)


def test_object_form_check_with_junit_is_accepted(products_dir):
    factory_yaml = products_dir / "backend-service" / "factory.yaml"
    doc = yaml.safe_load(factory_yaml.read_text())
    doc["checks"]["unit"] = {"command": "python -m unittest discover -s tests -t .",
                             "junit": "reports/unit.xml", "min_tests": 1}
    factory_yaml.write_text(yaml.safe_dump(doc))
    registry = ProductRegistry(products_dir, KIT_DIR)
    product = registry.get("backend-service")
    assert product.checks["unit"]["junit"] == "reports/unit.xml"
    assert product.checks["unit"]["min_tests"] == 1
    assert product.checks["unit"]["argv"] == ["python", "-m", "unittest", "discover", "-s", "tests", "-t", "."]


def test_missing_check_command_for_a_required_check_fails_closed(products_dir):
    factory_yaml = products_dir / "backend-service" / "factory.yaml"
    doc = yaml.safe_load(factory_yaml.read_text())
    doc["verification"]["required"] = ["lint", "unit", "types"]  # "types" has no `checks` entry
    factory_yaml.write_text(yaml.safe_dump(doc))
    with pytest.raises(ProductConfigError):
        ProductRegistry(products_dir, KIT_DIR)


def test_review_agent_and_holdout_blackbox_need_no_command(products_dir):
    factory_yaml = products_dir / "backend-service" / "factory.yaml"
    doc = yaml.safe_load(factory_yaml.read_text())
    doc["verification"]["required"] = ["lint", "unit", "review_agent", "holdout_blackbox"]
    factory_yaml.write_text(yaml.safe_dump(doc))
    registry = ProductRegistry(products_dir, KIT_DIR)
    assert "review_agent" not in registry.get("backend-service").checks


def test_invalid_schema_fails_closed(products_dir):
    factory_yaml = products_dir / "backend-service" / "factory.yaml"
    doc = yaml.safe_load(factory_yaml.read_text())
    del doc["risk_profile"]
    factory_yaml.write_text(yaml.safe_dump(doc))
    with pytest.raises(ProductConfigError):
        ProductRegistry(products_dir, KIT_DIR)


def test_no_products_found_fails_closed(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ProductConfigError):
        ProductRegistry(empty, KIT_DIR)
