"""Product registry: loads `<products_dir>/<product>/factory.yaml` (+ mandates/*.yaml)
and validates both against the kit schemas (build spec §3 B7).

Invalid product configuration fails loudly at startup -- a product cell with a
broken `factory.yaml` or a standing mandate that can't cover anything is a
configuration bug, not something the pipeline should paper over at runtime.
"""
from __future__ import annotations

import json
import shlex
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

import jsonschema
import yaml

from ..controller.coverage import StandingMandate

# Every check in `verification.required` needs a sandbox command in `checks`,
# except these two: the reviewer runs as an agent (verification.review) and the
# holdout runs in a separate repo (verification.holdout) -- neither is a shell command.
NO_COMMAND_REQUIRED = frozenset({"review_agent", "holdout_blackbox"})


class ProductConfigError(ValueError):
    """A product's `factory.yaml` or a mandate failed schema validation or a cross-check."""


def _load_yaml(path: Path) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def _load_schema(kit_dir: Path, name: str) -> dict:
    with open(kit_dir / "schemas" / name) as f:
        return json.load(f)


def _parse_window(text: str) -> timedelta:
    if text.endswith("h"):
        return timedelta(hours=int(text[:-1]))
    if text.endswith("d"):
        return timedelta(days=int(text[:-1]))
    raise ProductConfigError(f"observation_window {text!r} must end in 'h' or 'd'")


def _normalize_check(spec) -> dict:
    """Normalize one `checks.<name>` entry to `{command, argv, junit, min_tests}`.

    `factory.yaml` allows either a plain command string or `{command, junit,
    min_tests}` (junit adds a JUnit-XML pass/fail gate on top of the exit
    code -- see the schema's `checks` description). Both forms keep `argv`
    (the exit-code-only view `verification.local_checks.run_local_checks`
    takes today) so callers don't need to branch on the input shape.
    """
    # `junit`/`min_tests` are left OUT entirely rather than set to `None`: they are
    # passed straight through to `verification.local_checks.run_local_checks`,
    # whose `spec.get("min_tests", DEFAULT_MIN_TESTS)` only applies its default
    # when the key is absent, not when it's present-but-`None`.
    if isinstance(spec, str):
        return {"command": spec, "argv": shlex.split(spec)}
    normalized = {"command": spec["command"], "argv": shlex.split(spec["command"])}
    if "junit" in spec:
        normalized["junit"] = spec["junit"]
    if "min_tests" in spec:
        normalized["min_tests"] = spec["min_tests"]
    return normalized


def _owner_roles(owners: dict[str, str]) -> dict[str, tuple]:
    """Invert `owners` (role -> @login) into (login -> roles), for approver rosters.

    A login can hold more than one role (role collapsing, final draft §6.3):
    e.g. the same person may be both `product` and `tech_lead` in a small team.
    """
    roles: dict[str, list] = {}
    for role, login in owners.items():
        roles.setdefault(login, []).append(role)
    return {login: tuple(sorted(r)) for login, r in roles.items()}


@dataclass(frozen=True)
class Product:
    """One product cell's profile (`factory.yaml`), fully parsed and validated."""
    name: str
    risk_profile: str
    autonomy_level: str
    kit_version: str
    policy_version: str
    runtime: str
    models: dict
    owners: dict
    owner_roles: dict          # login -> tuple[role, ...]
    slack_ids: dict            # @login -> Slack user id (factory.yaml `slack_ids`; may be empty)
    approvers: dict            # gate -> list[str] | "codeowners"
    lanes: tuple
    protected_paths: tuple
    forbidden_paths: tuple
    gate_overrides: dict
    budgets: dict
    repair_attempts: dict
    infra_retries: int
    verification_required: tuple
    mutation: str
    checks: dict                # name -> {"command", "argv", "junit", "min_tests"} (see `_normalize_check`)
    repo: str | None
    holdout_repo: str | None
    observation_window: timedelta
    standing_mandates: tuple    # tuple[StandingMandate, ...]
    root_dir: Path

    def approvers_for(self, gate: str) -> tuple:
        """Resolve the approver-login list for `gate` (see `eligible_for` for the
        authoritative approver -> roles mapping an `ApprovalRequest` is built
        from; this is the plain login list, used e.g. to pick a default approver
        in the demo)."""
        return tuple(self.eligible_for(gate).keys())

    def roles_for(self, login: str) -> tuple:
        return self.owner_roles.get(login, ())

    def eligible_for(self, gate: str) -> dict:
        """Approver login -> roles, from THIS product's `factory.yaml`, for exactly
        this gate (red team #3 H2). This is what `ApprovalRequest.eligible` is built
        from: `check_decision` then rejects any approver not in this mapping and
        uses these roles regardless of what a caller (e.g. an inbox token) claims,
        so approval eligibility is never a global union across products and never
        trusts a claimed role over the product's own config.

        `hm: codeowners` resolves to the owners holding the `tech_lead` or
        `security` role (not `product`): those are the two roles the golden-path
        CODEOWNERS file lists as reviewers in v1, since the pipeline doesn't parse
        CODEOWNERS itself.
        """
        value = self.approvers.get(gate.lower())
        if value == "codeowners":
            logins = [login for login, roles in self.owner_roles.items()
                     if "tech_lead" in roles or "security" in roles]
        else:
            logins = list(value or ())
        return {login: self.owner_roles.get(login, ()) for login in logins}

    def mandate_for(self, *, labels) -> StandingMandate | None:
        """The first standing mandate whose label requirement `labels` could satisfy."""
        label_set = set(labels)
        for mandate in self.standing_mandates:
            if not mandate.labels_any or label_set & set(mandate.labels_any):
                return mandate
        return None


class ProductRegistry:
    """Loads and indexes every product cell under `products_dir`."""

    def __init__(self, products_dir: str | Path, kit_dir: str | Path):
        self._products_dir = Path(products_dir)
        self._kit_dir = Path(kit_dir)
        self._factory_schema = _load_schema(self._kit_dir, "factory.schema.json")
        self._mandate_schema = _load_schema(self._kit_dir, "mandate.schema.json")
        self._by_name: dict[str, Product] = {}
        self._by_repo: dict[str, str] = {}
        self._load_all()

    def _load_all(self) -> None:
        if not self._products_dir.is_dir():
            raise ProductConfigError(f"products_dir {self._products_dir} does not exist")
        for entry in sorted(self._products_dir.iterdir()):
            factory_yaml = entry / "factory.yaml"
            if entry.is_dir() and factory_yaml.is_file():
                product = self._load_product(entry, factory_yaml)
                self._by_name[product.name] = product
                if product.repo:
                    self._by_repo[product.repo] = product.name
        if not self._by_name:
            raise ProductConfigError(f"no product found under {self._products_dir} (expected <product>/factory.yaml)")

    def _load_product(self, root: Path, factory_yaml: Path) -> Product:
        doc = _load_yaml(factory_yaml)
        try:
            jsonschema.validate(instance=doc, schema=self._factory_schema)
        except jsonschema.ValidationError as exc:
            raise ProductConfigError(f"{factory_yaml}: {exc.message}") from exc

        checks_raw = doc.get("checks", {})
        checks = {name: _normalize_check(spec) for name, spec in checks_raw.items()}
        required = tuple(doc["verification"]["required"])
        missing_commands = [name for name in required if name not in NO_COMMAND_REQUIRED and name not in checks]
        if missing_commands:
            raise ProductConfigError(
                f"{factory_yaml}: verification.required names {missing_commands} with no `checks` command")

        # Verification floor (red team #3 item 15): a product can add checks, but
        # can never drop the reviewer or (outside `experimental`) the holdout below
        # what final draft §9.2 requires as the pre-merge/pre-release gates.
        risk_profile = doc["risk_profile"]
        floor_missing = []
        if "review_agent" not in required:
            floor_missing.append("review_agent")
        if risk_profile in ("standard", "regulated") and "holdout_blackbox" not in required:
            floor_missing.append("holdout_blackbox")
        if floor_missing:
            raise ProductConfigError(
                f"{factory_yaml}: verification.required is missing the floor requirement(s) "
                f"{floor_missing} (review_agent is always required; holdout_blackbox is required "
                f"for standard/regulated products, this one is {risk_profile!r})")

        protected = tuple(doc.get("protected_paths", ()))
        forbidden = tuple(doc.get("forbidden_paths", ()))
        mandates = tuple(self._load_mandates(root, doc.get("standing_mandates", ()),
                                             product_protected=protected, product_forbidden=forbidden))

        return Product(
            name=doc["product"],
            risk_profile=doc["risk_profile"],
            autonomy_level=doc["autonomy_level"],
            kit_version=doc["kit"]["version"],
            policy_version=doc["kit"]["policy_version"],
            runtime=doc["runtime"],
            models=dict(doc["models"]),
            owners=dict(doc["owners"]),
            owner_roles=_owner_roles(doc["owners"]),
            slack_ids=dict(doc.get("slack_ids", {})),
            approvers=dict(doc["approvers"]),
            lanes=tuple(doc["lanes"]),
            protected_paths=protected,
            forbidden_paths=forbidden,
            gate_overrides=dict(doc.get("gates", {})),
            budgets=dict(doc["budgets"]),
            repair_attempts=dict(doc["repair_attempts"]),
            infra_retries=int(doc.get("infra_retries", 3)),
            verification_required=required,
            mutation=doc["verification"].get("mutation", "off"),
            checks=checks,
            repo=doc.get("repo"),
            holdout_repo=doc.get("holdout_repo"),
            observation_window=_parse_window(doc.get("observation_window", "24h")),
            standing_mandates=mandates,
            root_dir=root,
        )

    def _load_mandates(self, root: Path, names, *, product_protected, product_forbidden):
        mandates_dir = root / "mandates"
        for mandate_id in names:
            path = mandates_dir / f"{mandate_id}.yaml"
            if not path.is_file():
                raise ProductConfigError(f"{root}: standing_mandates names {mandate_id!r}, no {path}")
            doc = _load_yaml(path)
            try:
                jsonschema.validate(instance=doc, schema=self._mandate_schema)
            except jsonschema.ValidationError as exc:
                raise ProductConfigError(f"{path}: {exc.message}") from exc
            if doc["mandate_id"] != mandate_id:
                raise ProductConfigError(f"{path}: mandate_id {doc['mandate_id']!r} != file name {mandate_id!r}")
            yield StandingMandate.from_dict(doc, product_protected=product_protected,
                                            product_forbidden=product_forbidden)

    def get(self, name: str) -> Product:
        try:
            return self._by_name[name]
        except KeyError:
            raise ProductConfigError(f"unknown product {name!r}") from None

    def for_repo(self, repo: str) -> Product:
        name = self._by_repo.get(repo)
        if name is None:
            raise ProductConfigError(f"repo {repo!r} is not registered to any product")
        return self._by_name[name]

    def all(self) -> tuple:
        return tuple(self._by_name.values())


__all__ = ["Product", "ProductConfigError", "ProductRegistry"]
