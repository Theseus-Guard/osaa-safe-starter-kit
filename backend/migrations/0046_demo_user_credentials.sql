-- Demo sign-in per role (F2, package P12; plans/06 demo security boundary, plans/f0/threat-model.md TM-02 and TM-33).
--
-- Each demo user has its own random credential. The credential is written once to a file in the directory named by
-- SAFE_DEMO_CREDENTIAL_DIR; the database keeps only its Argon2 hash, in this table. Demo users keep an unusable
-- password in users.password_hash, so the ordinary sign-in route never accepts a demo credential.
--
-- The marker row gets a seed version: 1 is the first seed (demo administrator only), 2 adds the credentials of the other
-- demo roles and their case assignments. Existing installs upgrade with `python -m safe_kit.demo_seed upgrade`.
--
-- The demo administrator keeps the credential it already has. Its hash is copied from the marker row here, so the same
-- plain value still signs in; the marker row keeps its own copy and nothing is deleted.
--
-- The table keeps the standard columns; the application never deletes rows. Grants come from the default privileges set
-- up for the migrator role (db/init), so this migration adds none.

ALTER TABLE demo_dataset
    ADD COLUMN seed_version integer NOT NULL DEFAULT 1 CHECK (seed_version BETWEEN 1 AND 2);

CREATE TABLE demo_user_credentials (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users (id),
    -- Argon2 hash of the demo credential, the same format as users.password_hash. The plain value is written once to the
    -- file named by SAFE_DEMO_CREDENTIAL_DIR (one file per user) and is not stored.
    credential_hash text NOT NULL CHECK (char_length(credential_hash) BETWEEN 1 AND 1024),
    seeded_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT demo_user_credentials_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

-- One active credential per demo user. The seed never replaces a credential; a second row would need a new decision.
CREATE UNIQUE INDEX demo_user_credentials_active_unique ON demo_user_credentials (user_id) WHERE NOT is_deleted;

CREATE TRIGGER demo_user_credentials_set_updated_at BEFORE UPDATE ON demo_user_credentials FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Backfill for an existing install: the demo administrator (fixed ID from safe_kit.demo_seed) keeps the hash it already has.
INSERT INTO demo_user_credentials (user_id, credential_hash, seeded_at)
SELECT u.id, d.credential_hash, d.seeded_at
FROM demo_dataset d
JOIN users u ON u.id = '5afe0d00-0000-4000-8000-000000000001' AND NOT u.is_deleted
WHERE d.dataset = 'synthetic-demo' AND NOT d.is_deleted;
