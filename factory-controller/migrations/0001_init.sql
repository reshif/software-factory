-- 0001_init.sql: core state-store schema (final draft §10, §12.1, §12.2).
--
-- Idempotent: every statement uses IF NOT EXISTS, so re-running this file is
-- harmless. factory.store.postgres.apply_migrations() also tracks which
-- migration files it has already run in schema_migrations, so this file
-- normally executes at most once per database regardless.

CREATE TABLE IF NOT EXISTS missions (
    id BIGSERIAL,
    mission_id TEXT PRIMARY KEY,
    product TEXT NOT NULL,
    repo TEXT NOT NULL,
    work_item_id TEXT NOT NULL,
    lane TEXT NOT NULL,
    risk_profile TEXT NOT NULL,
    autonomy_level TEXT NOT NULL,
    kit_version TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'NEW',
    state_version INTEGER NOT NULL DEFAULT 0,
    held_from TEXT,
    action_class TEXT,
    mandate_id TEXT,
    budget_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    spent_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    base_commit TEXT,
    branch TEXT,
    pr_number INTEGER,
    content_hash TEXT,
    artifact TEXT,
    flag TEXT,
    editors JSONB NOT NULL DEFAULT '[]',
    created_at TIMESTAMPTZ,
    updated_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS missions_id_idx ON missions (id);
CREATE INDEX IF NOT EXISTS missions_work_item_id_idx ON missions (work_item_id);

CREATE TABLE IF NOT EXISTS tasks (
    id BIGSERIAL,
    task_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    contract JSONB NOT NULL,
    state TEXT NOT NULL DEFAULT 'WAITING_DEPS',
    depends_on JSONB NOT NULL DEFAULT '[]',
    repair_attempts_used INTEGER NOT NULL DEFAULT 0,
    infra_retries_used INTEGER NOT NULL DEFAULT 0,
    session_id TEXT,
    revision TEXT,
    spent_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    updated_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS tasks_id_idx ON tasks (id);
CREATE INDEX IF NOT EXISTS tasks_mission_id_idx ON tasks (mission_id);

CREATE TABLE IF NOT EXISTS evidence (
    id BIGSERIAL,
    mission_id TEXT NOT NULL,
    revision TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    diff_ref TEXT NOT NULL,
    checks JSONB NOT NULL,
    action_class TEXT NOT NULL,
    rule_fired TEXT NOT NULL,
    task_ids JSONB NOT NULL DEFAULT '[]',
    holdout JSONB,
    blast_radius JSONB NOT NULL DEFAULT '[]',
    rollback_plan TEXT,
    rollback_tested BOOLEAN NOT NULL DEFAULT FALSE,
    cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    tokens INTEGER NOT NULL DEFAULT 0,
    ci_minutes DOUBLE PRECISION NOT NULL DEFAULT 0,
    untrusted_inputs JSONB NOT NULL DEFAULT '[]',
    agent_versions JSONB NOT NULL DEFAULT '{}',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (mission_id, revision)
);
CREATE INDEX IF NOT EXISTS evidence_mission_id_idx ON evidence (mission_id, id);

CREATE TABLE IF NOT EXISTS packets (
    request_id TEXT PRIMARY KEY,
    gate TEXT NOT NULL,
    mission_ids JSONB NOT NULL,
    title TEXT NOT NULL,
    recommendation TEXT NOT NULL,
    summary TEXT NOT NULL,
    required TEXT NOT NULL,
    expires TIMESTAMPTZ NOT NULL,
    content_hash TEXT NOT NULL,
    raw_diff TEXT NOT NULL DEFAULT '',
    alternatives JSONB NOT NULL DEFAULT '[]',
    evidence JSONB,
    recovery_plan TEXT,
    cost_usd DOUBLE PRECISION NOT NULL DEFAULT 0,
    untrusted_inputs JSONB NOT NULL DEFAULT '[]',
    links JSONB NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS events (
    id BIGSERIAL PRIMARY KEY,
    mission_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS events_mission_id_idx ON events (mission_id, id);

-- Fencing tokens for approval consumption (final draft §10): a durable,
-- globally monotonic counter, so a stale executor can never win a race even
-- across controller restarts.
CREATE SEQUENCE IF NOT EXISTS approval_fencing_seq START 1;

CREATE TABLE IF NOT EXISTS approvals (
    id BIGSERIAL,
    request_id TEXT PRIMARY KEY,
    gate TEXT NOT NULL,
    mission_ids JSONB NOT NULL,
    operation_id TEXT NOT NULL,
    artifact TEXT,
    content_hash TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    state_version INTEGER NOT NULL,
    required_kind TEXT NOT NULL,
    required_approvals INTEGER NOT NULL DEFAULT 0,
    required_security BOOLEAN NOT NULL DEFAULT FALSE,
    required_sampled BOOLEAN NOT NULL DEFAULT FALSE,
    risk_profile TEXT NOT NULL,
    requester TEXT NOT NULL,
    expires TIMESTAMPTZ NOT NULL,
    editors JSONB NOT NULL DEFAULT '[]',
    nonce TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    consumed_by TEXT,
    consumed_at TIMESTAMPTZ,
    fencing_token BIGINT
);
CREATE INDEX IF NOT EXISTS approvals_id_idx ON approvals (id);
CREATE INDEX IF NOT EXISTS approvals_status_idx ON approvals (status);

CREATE TABLE IF NOT EXISTS approval_decisions (
    id BIGSERIAL PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES approvals (request_id),
    approver TEXT NOT NULL,
    roles JSONB NOT NULL,
    decision TEXT NOT NULL,
    decided_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS approval_decisions_request_id_idx ON approval_decisions (request_id, id);

CREATE TABLE IF NOT EXISTS intents (
    id BIGSERIAL,
    operation_id TEXT PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'pending',
    receipt TEXT
);
CREATE INDEX IF NOT EXISTS intents_id_idx ON intents (id);
