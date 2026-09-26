"""Build the evidence bundle bound to a diff (final draft §10, §12.1).

`build_evidence` writes the raw diff to disk under `evidence_dir` and returns an
`EvidenceBundle` whose `diff_ref` points at it. `EvidenceBundle.to_dict()` (in
`models.py`) must validate against `factory-kit/schemas/evidence-bundle.schema.json`;
that's asserted in the tests here, not re-implemented.
"""
from pathlib import Path

from ..models import CheckResult, Diff, EvidenceBundle


def _diff_path(evidence_dir: str, mission_id: str, diff: Diff) -> Path:
    digest = diff.content_hash.removeprefix("sha256:")
    return Path(evidence_dir) / mission_id / f"{digest}.diff"


def build_evidence(*, mission_id: str, revision: str, diff: Diff, checks: dict[str, CheckResult],
                    action_class: str, rule_fired: str, evidence_dir: str,
                    task_ids: tuple = (), holdout: tuple | None = None, blast_radius: tuple = (),
                    rollback_plan: str | None = None, rollback_tested: bool = False,
                    cost_usd: float = 0.0, tokens: int = 0, ci_minutes: float = 0.0,
                    untrusted_inputs: tuple = (), agent_versions: dict | None = None) -> EvidenceBundle:
    """Write `diff.patch` under `evidence_dir` and build the bundle that references it.

    The file is written at `evidence_dir/<mission_id>/<content-hash>.diff`, so re-running
    for the same diff is idempotent (same path, same content).
    """
    path = _diff_path(evidence_dir, mission_id, diff)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(diff.patch)

    return EvidenceBundle(
        mission_id=mission_id,
        revision=revision,
        content_hash=diff.content_hash,
        diff_ref=str(path),
        checks=dict(checks),
        action_class=action_class,
        rule_fired=rule_fired,
        task_ids=tuple(task_ids),
        holdout=tuple(holdout) if holdout is not None else None,
        blast_radius=tuple(blast_radius),
        rollback_plan=rollback_plan,
        rollback_tested=rollback_tested,
        cost_usd=cost_usd,
        tokens=tokens,
        ci_minutes=ci_minutes,
        untrusted_inputs=tuple(untrusted_inputs),
        agent_versions=dict(agent_versions) if agent_versions else {},
    )
