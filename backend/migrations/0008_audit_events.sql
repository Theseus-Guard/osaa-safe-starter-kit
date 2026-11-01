-- Append-only audit trail. Rows are written by the application, never edited, and never removed.
-- The trigger below rejects every update except setting is_deleted (the soft delete itself).
-- details must never contain passwords, tokens, or IP addresses; the application enforces this before the insert.

CREATE TABLE audit_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_id uuid REFERENCES users (id),
    action text NOT NULL,
    target_type text NOT NULL,
    target_id text,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT audit_events_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE TRIGGER audit_events_set_updated_at BEFORE UPDATE ON audit_events FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE FUNCTION reject_audit_event_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id, NEW.actor_id, NEW.action, NEW.target_type, NEW.target_id, NEW.details, NEW.created_at)
        IS DISTINCT FROM (OLD.id, OLD.actor_id, OLD.action, OLD.target_type, OLD.target_id, OLD.details, OLD.created_at) THEN
        RAISE EXCEPTION 'audit events are append-only: their content cannot be changed';
    END IF;
    -- The only allowed change is the soft delete: is_deleted goes from false to true.
    IF NOT (NOT OLD.is_deleted AND NEW.is_deleted) AND (
        NEW.is_deleted IS DISTINCT FROM OLD.is_deleted OR NEW.deleted_at IS DISTINCT FROM OLD.deleted_at
    ) THEN
        RAISE EXCEPTION 'audit events are append-only: a soft delete cannot be undone or re-dated';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER audit_events_append_only BEFORE UPDATE ON audit_events FOR EACH ROW EXECUTE FUNCTION reject_audit_event_change();
