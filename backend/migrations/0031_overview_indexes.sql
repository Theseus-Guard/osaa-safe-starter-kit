-- Indexes for the overview dashboard (F2). The dashboard counts rows in a date range (7, 30 or 90 days).
-- These date columns had no index for that: cases and audit events were indexed only by creator or target, and the
-- login sessions only by family. The partial predicates match the NOT is_deleted reads used by the queries.
-- Forward only: this migration adds indexes and nothing else.

CREATE INDEX cases_created_at_idx ON cases (created_at) WHERE NOT is_deleted;
CREATE INDEX audit_events_created_at_idx ON audit_events (created_at) WHERE NOT is_deleted;
CREATE INDEX refresh_tokens_created_at_idx ON refresh_tokens (created_at) WHERE NOT is_deleted;
CREATE INDEX outbox_jobs_updated_at_idx ON outbox_jobs (updated_at) WHERE NOT is_deleted;
