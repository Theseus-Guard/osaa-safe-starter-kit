-- Password expiry and password reuse history.
-- password_changed_at is set whenever a password is set: account creation, admin reset, and self-service change.
-- A password older than 180 days forces a change at login. Existing accounts start their 180 days now.
-- password_history keeps an Argon2id hash of every password an account has had, temporary ones included.
-- The last three entries are checked when a new password is chosen. The current hash of each existing account
-- is copied in as its first history entry. Every table keeps the standard columns; the application never deletes rows.

ALTER TABLE users
    ADD COLUMN password_changed_at timestamptz NOT NULL DEFAULT now();

-- The default only fills existing rows. Every write that sets a password sets this column explicitly.
ALTER TABLE users
    ALTER COLUMN password_changed_at DROP DEFAULT;

CREATE TABLE password_history (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users (id),
    password_hash text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT password_history_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE INDEX password_history_user_idx ON password_history (user_id, created_at) WHERE NOT is_deleted;

CREATE TRIGGER password_history_set_updated_at BEFORE UPDATE ON password_history FOR EACH ROW EXECUTE FUNCTION set_updated_at();

INSERT INTO password_history (user_id, password_hash)
SELECT id, password_hash FROM users;
