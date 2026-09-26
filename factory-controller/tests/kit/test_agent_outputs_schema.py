"""agent-outputs.schema.json must accept exactly the final-draft §4 examples (spec §3, §4 B6)."""
import json

import jsonschema
import pytest

# The exact examples from docs/software-factory-final-draft.md §4 / phase2-build-spec.md §4.
EXAMPLES = {
    "intake": {
        "lane": "patch",
        "suggested_class": "AC3",
        "summary": "Fix an off-by-one error in the pagination helper.",
        "risk_notes": [],
    },
    "architect": {
        "summary": "Add rate limiting to the billing API.",
        "recommendation": "APPROVE: scoped, testable, no protected paths touched.",
        "acceptance_criteria": ["WHEN a client exceeds 100 req/min THE SYSTEM SHALL respond 429."],
        "tasks": [
            {
                "task_id": "T-1", "objective": "Add a token-bucket limiter middleware.",
                "owned_paths": ["src/x/**"], "action_class": "AC4",
                "acceptance_checks": ["python -m pytest tests/test_x.py"], "depends_on": [],
            }
        ],
        "alternatives": ["Use a third-party rate-limit service (rejected: new dependency, AC7)."],
        "risks": ["Limiter could reject legitimate bursty clients."],
        "recovery_plan": "Disable the feature flag; no data impact.",
        "estimate_usd": 12.5,
    },
    "implementer": {
        "status": "done",
        "summary": "Implemented the token-bucket limiter and its tests.",
        "tests_added": ["tests/test_ratelimit.py"],
        "commands_run": ["python -m pytest tests/test_ratelimit.py"],
        "blocker": None,
    },
    "qa": {
        "status": "pass",
        "scenarios": [{"name": "burst of 150 requests", "result": "pass", "detail": "429 after request 101"}],
    },
    "reviewer": {
        "verdict": "pass",
        "findings": [],
    },
}


@pytest.fixture(scope="module")
def schema_doc(kit_dir):
    return json.loads((kit_dir / "schemas" / "agent-outputs.schema.json").read_text())


def test_schema_itself_is_valid(schema_doc):
    jsonschema.Draft202012Validator.check_schema(schema_doc)


@pytest.mark.parametrize("role,example", EXAMPLES.items())
def test_role_defs_accept_the_final_draft_examples(schema_doc, role, example):
    subschema = schema_doc["$defs"][role]
    validator = jsonschema.Draft202012Validator(subschema)
    errors = sorted(validator.iter_errors(example), key=lambda e: e.path)
    assert not errors, [f"{role}: {list(e.path)}: {e.message}" for e in errors]


@pytest.mark.parametrize("role,example", EXAMPLES.items())
def test_envelope_form_is_also_accepted(schema_doc, role, example):
    """The optional versioned envelope (schema_version/role/output) also validates."""
    envelope = {"schema_version": 1, "role": role, "output": example}
    validator = jsonschema.Draft202012Validator(schema_doc)
    errors = sorted(validator.iter_errors(envelope), key=lambda e: e.path)
    assert not errors, [f"{role}: {list(e.path)}: {e.message}" for e in errors]


def test_reviewer_rejects_a_blocking_finding_missing_fields(schema_doc):
    bad = {"verdict": "fail", "findings": [{"severity": "blocking"}]}
    validator = jsonschema.Draft202012Validator(schema_doc["$defs"]["reviewer"])
    assert list(validator.iter_errors(bad))


def test_intake_rejects_ac8(schema_doc):
    bad = dict(EXAMPLES["intake"], suggested_class="AC8")
    validator = jsonschema.Draft202012Validator(schema_doc["$defs"]["intake"])
    assert list(validator.iter_errors(bad))


def test_architect_rejects_a_malformed_recommendation(schema_doc):
    bad = dict(EXAMPLES["architect"], recommendation="LGTM")
    validator = jsonschema.Draft202012Validator(schema_doc["$defs"]["architect"])
    assert list(validator.iter_errors(bad))
