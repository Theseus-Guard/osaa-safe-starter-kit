-- Redaction and TLP audience (F2, package P4).
--
-- A redaction creates a derived evidence item: a new evidence_items row with its own ID and its own encrypted object.
-- The original row and its object are never changed. The derived item carries a TLP audience label, and the manifest
-- of what was removed and why is kept in evidence_redactions. Claims (review layers that cite the original) are
-- assessed on the derived item in evidence_claim_assessments; the outcome is insufficient_evidence, as in the review
-- model. A release of CLEAR material needs a row in evidence_release_approvals; the approval flow itself is a later package.
-- Every table keeps the standard columns; the application never deletes rows. Grants come from the default privileges
-- set up for the migrator role (db/init), so this migration adds none.

-- 1. TLP label. NULL for uploaded originals, which have no TLP rule of their own. A derived item always has one.
ALTER TABLE evidence_items ADD COLUMN tlp_label text
    CHECK (tlp_label IS NULL OR tlp_label IN ('RED', 'AMBER', 'AMBER+STRICT', 'GREEN', 'CLEAR'));

-- 2. Content of an evidence item cannot change after it is written. Only the status, the checksums (set once, when the
--    upload is verified), the soft delete, and updated_at may change. The original of a redaction therefore stays as it was.
CREATE FUNCTION reject_evidence_content_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id, NEW.case_id, NEW.uploaded_by, NEW.original_filename, NEW.media_type, NEW.size_bytes,
        NEW.storage_key, NEW.key_id, NEW.wrapped_data_key, NEW.nonce, NEW.tlp_label)
        IS DISTINCT FROM
       (OLD.id, OLD.case_id, OLD.uploaded_by, OLD.original_filename, OLD.media_type, OLD.size_bytes,
        OLD.storage_key, OLD.key_id, OLD.wrapped_data_key, OLD.nonce, OLD.tlp_label) THEN
        RAISE EXCEPTION 'evidence content is immutable: only status and checksums may change';
    END IF;
    IF (OLD.sha256_plaintext IS NOT NULL AND NEW.sha256_plaintext IS DISTINCT FROM OLD.sha256_plaintext)
       OR (OLD.sha256_ciphertext IS NOT NULL AND NEW.sha256_ciphertext IS DISTINCT FROM OLD.sha256_ciphertext) THEN
        RAISE EXCEPTION 'evidence checksums are immutable once set';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER evidence_items_content_immutable BEFORE UPDATE ON evidence_items
    FOR EACH ROW EXECUTE FUNCTION reject_evidence_content_change();

-- 3. The chain of custody records a redaction on the original: custody_events is append-only, the new action is added.
ALTER TABLE custody_events DROP CONSTRAINT custody_events_action_check;
ALTER TABLE custody_events ADD CONSTRAINT custody_events_action_check
    CHECK (action IN ('uploaded', 'downloaded', 'verified', 'failed', 'redacted'));

-- 4. The manifest of one redaction: which original, which derived item, who, when, and the removed scope with reasons.
--    The removed text itself is never stored.
CREATE TABLE evidence_redactions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    original_evidence_id uuid NOT NULL REFERENCES evidence_items (id),
    derived_evidence_id uuid NOT NULL REFERENCES evidence_items (id),
    case_id uuid NOT NULL REFERENCES cases (id),
    actor_id uuid NOT NULL REFERENCES users (id),
    manifest jsonb NOT NULL CHECK (jsonb_typeof(manifest) = 'object'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT evidence_redactions_distinct_items CHECK (original_evidence_id <> derived_evidence_id),
    CONSTRAINT evidence_redactions_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

-- A derived item comes from exactly one redaction.
CREATE UNIQUE INDEX evidence_redactions_derived_unique ON evidence_redactions (derived_evidence_id) WHERE NOT is_deleted;
CREATE INDEX evidence_redactions_original_idx ON evidence_redactions (original_evidence_id) WHERE NOT is_deleted;

CREATE TRIGGER evidence_redactions_set_updated_at BEFORE UPDATE ON evidence_redactions FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE FUNCTION reject_redaction_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id, NEW.original_evidence_id, NEW.derived_evidence_id, NEW.case_id, NEW.actor_id, NEW.manifest, NEW.created_at)
        IS DISTINCT FROM (OLD.id, OLD.original_evidence_id, OLD.derived_evidence_id, OLD.case_id, OLD.actor_id, OLD.manifest, OLD.created_at) THEN
        RAISE EXCEPTION 'redaction records are append-only: their content cannot be changed';
    END IF;
    IF NOT (NOT OLD.is_deleted AND NEW.is_deleted) AND (
        NEW.is_deleted IS DISTINCT FROM OLD.is_deleted OR NEW.deleted_at IS DISTINCT FROM OLD.deleted_at
    ) THEN
        RAISE EXCEPTION 'redaction records are append-only: a soft delete cannot be undone or re-dated';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER evidence_redactions_append_only BEFORE UPDATE ON evidence_redactions
    FOR EACH ROW EXECUTE FUNCTION reject_redaction_change();

-- 5. Claims on the derived item. One row per review layer that cites the original and was set when the redaction ran.
CREATE TABLE evidence_claim_assessments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    redaction_id uuid NOT NULL REFERENCES evidence_redactions (id),
    derived_evidence_id uuid NOT NULL REFERENCES evidence_items (id),
    review_layer_id uuid NOT NULL REFERENCES review_layers (id),
    outcome text NOT NULL CHECK (outcome = 'insufficient_evidence'),
    reason text NOT NULL CHECK (char_length(reason) BETWEEN 1 AND 500),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT evidence_claim_assessments_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE UNIQUE INDEX evidence_claim_assessments_unique ON evidence_claim_assessments (derived_evidence_id, review_layer_id)
    WHERE NOT is_deleted;
CREATE INDEX evidence_claim_assessments_redaction_idx ON evidence_claim_assessments (redaction_id) WHERE NOT is_deleted;

CREATE TRIGGER evidence_claim_assessments_set_updated_at BEFORE UPDATE ON evidence_claim_assessments
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- 6. Human approval for releasing a CLEAR item. The row is written by the approval flow (a later package); until a row
--    exists, a CLEAR item is refused for every caller (safe_kit.tlp).
CREATE TABLE evidence_release_approvals (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    evidence_id uuid NOT NULL REFERENCES evidence_items (id),
    approved_by uuid NOT NULL REFERENCES users (id),
    approved_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT evidence_release_approvals_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE INDEX evidence_release_approvals_evidence_idx ON evidence_release_approvals (evidence_id) WHERE NOT is_deleted;

CREATE TRIGGER evidence_release_approvals_set_updated_at BEFORE UPDATE ON evidence_release_approvals
    FOR EACH ROW EXECUTE FUNCTION set_updated_at();
