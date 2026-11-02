-- Demo dataset marker (F2, package P12; plans/06 demo security boundary, plans/f0/threat-model.md TM-02 and TM-33).
--
-- One row marks a database as seeded with the synthetic demo dataset (safe-kit demo seed, safe_kit.demo_seed). It holds
-- the hash of the per-install demo credential, never the credential itself. Demo sign-in (safe_kit.demo_auth) accepts
-- the credential only when this row exists and the process runs in demo mode with the synthetic dataset. A production
-- database never gets the row, because the seed refuses to run outside demo mode.
--
-- The table keeps the standard columns; the application never deletes rows. Grants come from the default privileges set
-- up for the migrator role (db/init), so this migration adds none.

CREATE TABLE demo_dataset (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    dataset text NOT NULL CHECK (dataset = 'synthetic-demo'),
    -- Argon2 hash of the demo credential, the same format as users.password_hash. The plain value is written once to
    -- the credential file named by SAFE_DEMO_CREDENTIAL_FILE and is not stored.
    credential_hash text NOT NULL CHECK (char_length(credential_hash) BETWEEN 1 AND 1024),
    seeded_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT demo_dataset_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

-- At most one active marker per dataset: a second seed run finds the first row and writes nothing.
CREATE UNIQUE INDEX demo_dataset_active_unique ON demo_dataset (dataset) WHERE NOT is_deleted;

CREATE TRIGGER demo_dataset_set_updated_at BEFORE UPDATE ON demo_dataset FOR EACH ROW EXECUTE FUNCTION set_updated_at();
