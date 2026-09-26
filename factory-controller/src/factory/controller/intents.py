"""Write-ahead intents and receipts for side effects (final draft §10, §13.1 #9).

Durable execution doesn't make side effects exactly-once. So: record the intent
BEFORE acting, record the receipt AFTER, and on recovery reconcile a pending
intent against the real target state before retrying.
"""
from dataclasses import dataclass
from typing import Callable


@dataclass
class Intent:
    operation_id: str
    status: str = "pending"      # pending | done
    receipt: object = None


class IntentLog:
    """In-memory reference implementation. Phase 2 backs this with Postgres."""

    def __init__(self):
        self._intents: dict[str, Intent] = {}

    def get(self, operation_id: str) -> Intent | None:
        return self._intents.get(operation_id)

    def begin(self, operation_id: str) -> Intent:
        intent = self._intents.setdefault(operation_id, Intent(operation_id))
        return intent

    def complete(self, operation_id: str, receipt) -> None:
        intent = self._intents[operation_id]
        intent.status = "done"
        intent.receipt = receipt

    def pending(self) -> list[Intent]:
        return [i for i in self._intents.values() if i.status == "pending"]


def execute_once(log: IntentLog, operation_id: str, *, action: Callable[[], object],
                 probe: Callable[[], object | None]):
    """Run `action` at most once per operation id.

    `probe` inspects the real target and returns a receipt if the effect already happened.
    """
    intent = log.get(operation_id)
    if intent and intent.status == "done":
        return intent.receipt
    if intent and intent.status == "pending":
        observed = probe()
        if observed is not None:
            log.complete(operation_id, observed)
            return observed
    log.begin(operation_id)
    receipt = action()
    log.complete(operation_id, receipt)
    return receipt
