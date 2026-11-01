-- System timezone: the one display timezone of this installation, kept as an append-only history.
-- The application inserts a row for every change and never updates a row. The current timezone is the latest row
-- whose effective_from is not in the future. effective_from is the transaction time of the insert, never a client value.
-- Stored times stay in UTC everywhere; the timezone only decides how times are displayed.

CREATE TABLE system_timezone (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    timezone text NOT NULL,
    effective_from timestamptz NOT NULL DEFAULT now(),
    set_by uuid REFERENCES users (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT system_timezone_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE INDEX system_timezone_effective_idx ON system_timezone (effective_from DESC, created_at DESC) WHERE NOT is_deleted;

CREATE TRIGGER system_timezone_set_updated_at BEFORE UPDATE ON system_timezone FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Each case keeps the calendar timezone that was in effect when it was created, so later changes never move its deadlines.
-- Cases created before this migration were computed with the then hard-coded calendar, Europe/Istanbul.
ALTER TABLE cases ADD COLUMN calendar_timezone text;
UPDATE cases SET calendar_timezone = 'Europe/Istanbul' WHERE calendar_timezone IS NULL;
ALTER TABLE cases ALTER COLUMN calendar_timezone SET NOT NULL;
