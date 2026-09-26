"""`pipeline.products.ProductRegistry` (build spec §3 B7)."""
import shutil

import pytest
import yaml

from factory.pipeline.products import ProductConfigError, ProductRegistry
from factory.policy.loader import default_kit_dir

KIT_DIR = default_kit_dir()


@pytest.fixture
def products_dir(tmp_path):
    root = tmp_path / "products"
    root.mkdir()
    shutil.copytree(KIT_DIR / "templates" / "backend-service", root / "backend-service")
    return root


def test_loads_the_golden_path_template(products_dir):
    registry = ProductRegistry(products_dir, KIT_DIR)
    product = registry.get("backend-service")
    assert product.repo == "org/backend-service"
    assert product.risk_profile == "standard"
    assert product.checks["lint"]["argv"] == ["python", "-m", "compileall", "-q", "app", "tests"]
    assert len(product.standing_mandates) == 1
    assert product.standing_mandates[0].mandate_id == "SM-patch"


def test_repo_maps_back_to_the_product(products_dir):
    registry = ProductRegistry(products_dir, KIT_DIR)
    assert registry.for_repo("org/backend-service").name == "backend-service"


def test_for_repo_unregistered_raises(products_dir):
    registry = ProductRegistry(products_dir, KIT_DIR)
    with pytest.raises(ProductConfigError):
        registry.for_repo("someone/else")


def test_approvers_for_codeowners_hm_resolves_to_product_and_tech_lead(products_dir):
    registry = ProductRegistry(products_dir, KIT_DIR)
    product = registry.get("backend-service")
    assert set(product.approvers_for("HM")) == {"@po", "@tl"}
    assert set(product.approvers_for("hm")) == {"@po", "@tl"}


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
