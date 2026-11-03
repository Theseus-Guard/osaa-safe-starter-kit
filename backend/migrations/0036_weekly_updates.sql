-- Weekly machine-readable updates (plans/01, weekly status update; F2 package P9).
-- One row per update. The document holds the whole update in the project-defined JSON format
-- (schemas/weekly-update.v0.json). A row is written once and never changed; the application never deletes it.
-- The worker creates a row while a case has an open significant risk, at most one every 7 days (safe_kit.weekly_updates).
-- Every table keeps the standard columns; the application never deletes rows.

CREATE TABLE weekly_updates (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES cases (id),
    sequence integer NOT NULL CHECK (sequence >= 1),
    generated_at timestamptz NOT NULL,
    document jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT weekly_updates_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

-- One live update per sequence number of a case. The worker also holds a per-case lock while it numbers a new update.
CREATE UNIQUE INDEX weekly_updates_case_sequence_unique ON weekly_updates (case_id, sequence) WHERE NOT is_deleted;
CREATE INDEX weekly_updates_case_generated_idx ON weekly_updates (case_id, generated_at) WHERE NOT is_deleted;

CREATE TRIGGER weekly_updates_set_updated_at BEFORE UPDATE ON weekly_updates FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- The worker role gets no table privileges by default (migration 0012, db/init/10-roles.sh). It needs to read the
-- case, its significant impacts and its deadline tasks to build an update, insert the update, and write its audit row.
-- It takes a transaction advisory lock for the case, so it needs no UPDATE privilege. Each block runs only when the
-- role exists, so the migrations also run against a plain database in tests.
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'safe_worker') THEN
    GRANT SELECT ON cases, impacts, deadline_tasks, weekly_updates TO safe_worker;
    GRANT INSERT ON weekly_updates TO safe_worker;
    GRANT SELECT, INSERT ON audit_events TO safe_worker;
  END IF;
END
$$;
