-- Evidence files. The content is never stored in the database: it is an encrypted object in the evidence volume
-- (see src/safe_kit/evidence_storage.py). This table holds what is needed to find, decrypt, and verify that object:
-- the wrapped data key (never the plain key), the nonce, the key id, and two SHA-256 checksums.
--
-- Two-step upload: the row is inserted as pending, the encrypted object is written, then the row becomes available
-- with its checksums. If the write fails the row is marked failed. A pending row left behind by a crash is marked
-- failed by the reconciliation function. Nothing is deleted: the application never removes rows or objects.

CREATE TABLE evidence_items (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES cases (id),
    uploaded_by uuid NOT NULL REFERENCES users (id),
    -- Sanitized display name from the client. It is never used as a path and never used to look up an object.
    original_filename text NOT NULL CHECK (char_length(original_filename) BETWEEN 1 AND 255),
    media_type text NOT NULL,
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    -- Checksums are set when the row becomes available. They are integrity checks, not tamper-proofing.
    sha256_plaintext text CHECK (sha256_plaintext ~ '^[0-9a-f]{64}$'),
    sha256_ciphertext text CHECK (sha256_ciphertext ~ '^[0-9a-f]{64}$'),
    -- Random UUID chosen by the application. The object file is named after it.
    storage_key text NOT NULL UNIQUE,
    key_id text NOT NULL,
    wrapped_data_key bytea NOT NULL,
    nonce bytea NOT NULL,
    status text NOT NULL CHECK (status IN ('pending', 'available', 'failed')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT evidence_items_available_has_checksums CHECK (
        status <> 'available' OR (sha256_plaintext IS NOT NULL AND sha256_ciphertext IS NOT NULL)
    ),
    CONSTRAINT evidence_items_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE INDEX evidence_items_case_idx ON evidence_items (case_id, created_at, id) WHERE NOT is_deleted;

CREATE INDEX evidence_items_pending_idx ON evidence_items (created_at) WHERE status = 'pending' AND NOT is_deleted;

CREATE TRIGGER evidence_items_set_updated_at BEFORE UPDATE ON evidence_items FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Chain of custody: one row per upload, download, verification, or failure of an evidence item.
-- Append-only, like audit_events: the trigger below rejects every change except one soft delete.
-- details holds reasons and identifiers only, never file contents, keys, or file names.
-- actor_id is empty for system actions (the reconciliation of stale uploads).

CREATE TABLE custody_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    evidence_id uuid NOT NULL REFERENCES evidence_items (id),
    actor_id uuid REFERENCES users (id),
    action text NOT NULL CHECK (action IN ('uploaded', 'downloaded', 'verified', 'failed')),
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT custody_events_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE INDEX custody_events_evidence_idx ON custody_events (evidence_id, created_at, id) WHERE NOT is_deleted;

CREATE TRIGGER custody_events_set_updated_at BEFORE UPDATE ON custody_events FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE FUNCTION reject_custody_event_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id, NEW.evidence_id, NEW.actor_id, NEW.action, NEW.details, NEW.created_at)
        IS DISTINCT FROM (OLD.id, OLD.evidence_id, OLD.actor_id, OLD.action, OLD.details, OLD.created_at) THEN
        RAISE EXCEPTION 'custody events are append-only: their content cannot be changed';
    END IF;
    -- The only allowed change is the soft delete: is_deleted goes from false to true.
    IF NOT (NOT OLD.is_deleted AND NEW.is_deleted) AND (
        NEW.is_deleted IS DISTINCT FROM OLD.is_deleted OR NEW.deleted_at IS DISTINCT FROM OLD.deleted_at
    ) THEN
        RAISE EXCEPTION 'custody events are append-only: a soft delete cannot be undone or re-dated';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER custody_events_append_only BEFORE UPDATE ON custody_events FOR EACH ROW EXECUTE FUNCTION reject_custody_event_change();
