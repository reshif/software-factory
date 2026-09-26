-- 0002_webhooks_and_eligibility.sql: durable webhook inbox, use-once keys, and
-- approver eligibility on approvals (final draft §10; red team #3 H1/H2/M4).
-- Idempotent: safe to re-run. Applied under apply_migrations' advisory lock.

-- Approver -> roles from product config (red team #3 H2). NULL means
-- unrestricted (only used by unit tests of the bare rules).
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS eligible JSONB;

-- Durable webhook inbox shared by the `serve` and `worker` processes (red team
-- #3 H1, M4): a verified delivery is stored once, then leased to a worker
-- until it's acked or the lease expires (crash safety).
CREATE TABLE IF NOT EXISTS webhook_inbox (
    id BIGSERIAL,
    delivery_id TEXT PRIMARY KEY,
    event TEXT NOT NULL,
    payload JSONB NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    claimed_until TIMESTAMPTZ,
    acked_at TIMESTAMPTZ
);
-- id, not just received_at: a stable tiebreaker so "oldest first" is a total
-- order even when two deliveries land in the same clock tick.
CREATE INDEX IF NOT EXISTS webhook_inbox_claimable_idx
    ON webhook_inbox (received_at, id)
    WHERE acked_at IS NULL;

-- Atomic replay guard for one-time keys (e.g. an inbox token jti).
CREATE TABLE IF NOT EXISTS used_keys (
    key TEXT PRIMARY KEY,
    used_at TIMESTAMPTZ NOT NULL
);
