"""Shared update-key validation for `MissionRecord`/`TaskRecord` updates (`Q-M1`).

Both backends must reject the same malformed `**changes` the same way. This is
also what keeps `PostgresStateStore` from ever interpolating an arbitrary
caller-supplied key as a SQL column name: only real, mutable dataclass fields
get past `validate_update_fields` before any SQL is built.
"""
import dataclasses

# Fields the generic **changes dict may never name: identifiers, the CAS
# precondition, and the timestamps the store itself sets via its Clock.
MISSION_IMMUTABLE = ("mission_id", "state_version", "created_at", "updated_at")
TASK_IMMUTABLE = ("task_id", "updated_at")


def validate_update_fields(model: type, changes: dict, *, immutable: tuple[str, ...]) -> None:
    """Raise `TypeError` if `changes` names anything but a real, mutable field of `model`."""
    allowed = {f.name for f in dataclasses.fields(model)} - set(immutable)
    unknown = sorted(set(changes) - allowed)
    if unknown:
        raise TypeError(f"{model.__name__}: cannot update field(s) {unknown}")


__all__ = ["MISSION_IMMUTABLE", "TASK_IMMUTABLE", "validate_update_fields"]
