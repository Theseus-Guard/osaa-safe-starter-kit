-- Durable job queue (transactional outbox). Jobs are written in the same transaction as the change that needs them
-- and processed by the worker. Delivery is at least once: a handler may run again after a lost lease, so handlers must be idempotent.
-- Status flow: pending -> leased -> done, or leased -> pending (retry with backoff), or leased -> failed (attempts exhausted).

CREATE TABLE outbox_jobs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    job_type text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    status text NOT NULL CHECK (status IN ('pending', 'leased', 'done', 'failed')),
    attempts integer NOT NULL DEFAULT 0,
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_until timestamptz,
    last_error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT outbox_jobs_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE INDEX outbox_jobs_claim_idx ON outbox_jobs (status, available_at) WHERE NOT is_deleted;

CREATE TRIGGER outbox_jobs_set_updated_at BEFORE UPDATE ON outbox_jobs FOR EACH ROW EXECUTE FUNCTION set_updated_at();
