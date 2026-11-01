-- Every table gets the same audit columns. Rows are never deleted by the application:
-- a delete sets is_deleted = true and deleted_at = now(). Physical removal is a manual DBA action.
-- schema_migrations is excluded: it is the migration ledger maintained by the migration runner itself.

CREATE FUNCTION set_updated_at() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$;

ALTER TABLE users
    ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN deleted_at timestamptz,
    ADD COLUMN is_deleted boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT users_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL));
CREATE TRIGGER users_set_updated_at BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE refresh_tokens
    ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN deleted_at timestamptz,
    ADD COLUMN is_deleted boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT refresh_tokens_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL));
CREATE TRIGGER refresh_tokens_set_updated_at BEFORE UPDATE ON refresh_tokens FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE revoked_access_tokens
    ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN deleted_at timestamptz,
    ADD COLUMN is_deleted boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT revoked_access_tokens_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL));
CREATE TRIGGER revoked_access_tokens_set_updated_at BEFORE UPDATE ON revoked_access_tokens FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE rate_limits
    ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN deleted_at timestamptz,
    ADD COLUMN is_deleted boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT rate_limits_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL));
CREATE TRIGGER rate_limits_set_updated_at BEFORE UPDATE ON rate_limits FOR EACH ROW EXECUTE FUNCTION set_updated_at();

ALTER TABLE worker_heartbeat
    ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN deleted_at timestamptz,
    ADD COLUMN is_deleted boolean NOT NULL DEFAULT false,
    ADD CONSTRAINT worker_heartbeat_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL));
CREATE TRIGGER worker_heartbeat_set_updated_at BEFORE UPDATE ON worker_heartbeat FOR EACH ROW EXECUTE FUNCTION set_updated_at();
