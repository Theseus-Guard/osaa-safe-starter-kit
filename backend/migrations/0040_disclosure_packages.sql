-- Disclosure packages and human approval (F2, package P6; plans/02 disclosure flow, plans/f0/api-draft.md section 2.5).
--
-- A disclosure package is a set of redacted derivatives of one case, prepared for one recipient and one purpose. Each
-- package version is a new row in disclosure_versions with its evidence items (snapshots in disclosure_version_items),
-- a manifest, a digest over its canonical content, the TLP audience, and the template used. A changed package is a new
-- version; no version is ever edited.
--
-- An approval (disclosure_approvals) is a human decision bound to one version: the recipient, the purpose and the
-- digest the approver confirmed. A preparer may not approve their own version (the CHECK below), and the rule is
-- recorded on the row. A later version of the same package makes an earlier approval revoked by change: the send step
-- re-checks that the approved version is still the latest one and that its digest still matches its content.
--
-- Only derivatives are listed. The evidence items of a version are derived items (see evidence_redactions); an original
-- cannot be added, which the application checks and the database does not need to repeat.
--
-- Every table keeps the standard columns; the application never deletes rows. The four tables are insert-only: a
-- trigger rejects any change to their content, and only the soft-delete columns may change. Grants come from the
-- default privileges set up for the migrator role (db/init), so this migration adds none.

-- 1. Permissions. disclosures:approve is seeded by migration 0007 and granted to disclosure_approver.
INSERT INTO permissions (name, description) VALUES
    ('disclosures:read', 'Read disclosure package versions and their previews'),
    ('disclosures:prepare', 'Create disclosure package versions')
ON CONFLICT (name) DO NOTHING;

INSERT INTO role_permissions (role, permission) VALUES
    ('triager', 'disclosures:prepare'),
    ('triager', 'disclosures:read'),
    ('disclosure_approver', 'disclosures:read')
ON CONFLICT (role, permission) DO NOTHING;

-- 2. Insert-only content: an update may change only the soft-delete columns and updated_at.
CREATE FUNCTION reject_disclosure_content_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (to_jsonb(NEW) - ARRAY['updated_at', 'is_deleted', 'deleted_at']) IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['updated_at', 'is_deleted', 'deleted_at']) THEN
        RAISE EXCEPTION 'disclosure records are append-only: their content cannot be changed';
    END IF;
    IF NOT (NOT OLD.is_deleted AND NEW.is_deleted) AND (
        NEW.is_deleted IS DISTINCT FROM OLD.is_deleted OR NEW.deleted_at IS DISTINCT FROM OLD.deleted_at
    ) THEN
        RAISE EXCEPTION 'disclosure records are append-only: a soft delete cannot be undone or re-dated';
    END IF;
    RETURN NEW;
END;
$$;

-- 3. The package: the identity that groups its versions. A case may hold several packages.
CREATE TABLE disclosure_packages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES cases (id),
    created_by uuid NOT NULL REFERENCES users (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT disclosure_packages_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE INDEX disclosure_packages_case_idx ON disclosure_packages (case_id, created_at, id) WHERE NOT is_deleted;

CREATE TRIGGER disclosure_packages_set_updated_at BEFORE UPDATE ON disclosure_packages FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER disclosure_packages_append_only BEFORE UPDATE ON disclosure_packages FOR EACH ROW EXECUTE FUNCTION reject_disclosure_content_change();

-- 4. One version of a package. The digest is computed by the application over the canonical content (case, template,
--    TLP audience, recipient, purpose, the item snapshots, and the manifest). The manifest holds the removed line
--    ranges of each item and the checksum of its original; it never holds removed text or redaction reasons.
CREATE TABLE disclosure_versions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    package_id uuid NOT NULL REFERENCES disclosure_packages (id),
    case_id uuid NOT NULL REFERENCES cases (id),
    version integer NOT NULL CHECK (version >= 1),
    template text NOT NULL CHECK (template IN ('confidential_rapid_alert', 'de_identified_member_advisory', 'redacted_public_safety_report')),
    tlp_audience text NOT NULL CHECK (tlp_audience IN ('RED', 'AMBER', 'AMBER+STRICT', 'GREEN', 'CLEAR')),
    recipient text NOT NULL CHECK (char_length(recipient) BETWEEN 1 AND 200),
    purpose text NOT NULL CHECK (char_length(purpose) BETWEEN 1 AND 500),
    manifest jsonb NOT NULL CHECK (jsonb_typeof(manifest) = 'object'),
    digest text NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
    created_by uuid NOT NULL REFERENCES users (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT disclosure_versions_package_version_unique UNIQUE (package_id, version),
    CONSTRAINT disclosure_versions_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE INDEX disclosure_versions_case_idx ON disclosure_versions (case_id, created_at, id) WHERE NOT is_deleted;

CREATE TRIGGER disclosure_versions_set_updated_at BEFORE UPDATE ON disclosure_versions FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER disclosure_versions_append_only BEFORE UPDATE ON disclosure_versions FOR EACH ROW EXECUTE FUNCTION reject_disclosure_content_change();

-- 5. The evidence items of a version, as they were when the version was made. The snapshot keeps the version checkable
--    without reading mutable tables. Each item is a derived item of a redaction of the case.
CREATE TABLE disclosure_version_items (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    version_id uuid NOT NULL REFERENCES disclosure_versions (id),
    evidence_id uuid NOT NULL REFERENCES evidence_items (id),
    redaction_id uuid NOT NULL REFERENCES evidence_redactions (id),
    media_type text NOT NULL CHECK (char_length(media_type) BETWEEN 1 AND 200),
    size_bytes bigint NOT NULL CHECK (size_bytes >= 0),
    sha256_plaintext text NOT NULL CHECK (sha256_plaintext ~ '^[0-9a-f]{64}$'),
    tlp_label text NOT NULL CHECK (tlp_label IN ('RED', 'AMBER', 'AMBER+STRICT', 'GREEN', 'CLEAR')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT disclosure_version_items_unique UNIQUE (version_id, evidence_id),
    CONSTRAINT disclosure_version_items_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE INDEX disclosure_version_items_version_idx ON disclosure_version_items (version_id) WHERE NOT is_deleted;

CREATE TRIGGER disclosure_version_items_set_updated_at BEFORE UPDATE ON disclosure_version_items FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER disclosure_version_items_append_only BEFORE UPDATE ON disclosure_version_items FOR EACH ROW EXECUTE FUNCTION reject_disclosure_content_change();

-- 6. The human approval of one version. It names the recipient, the purpose, and the digest the approver confirmed, and
--    the preparer of the version. One approval per version: a changed package is a new version that needs its own.
CREATE TABLE disclosure_approvals (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    version_id uuid NOT NULL REFERENCES disclosure_versions (id),
    package_id uuid NOT NULL REFERENCES disclosure_packages (id),
    case_id uuid NOT NULL REFERENCES cases (id),
    recipient text NOT NULL CHECK (char_length(recipient) BETWEEN 1 AND 200),
    purpose text NOT NULL CHECK (char_length(purpose) BETWEEN 1 AND 500),
    digest text NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
    preparer_id uuid NOT NULL REFERENCES users (id),
    approved_by uuid NOT NULL REFERENCES users (id),
    approved_at timestamptz NOT NULL DEFAULT now(),
    -- The separation rule in force when the approval was made. Only this rule is defined so far.
    separation_rule text NOT NULL CHECK (separation_rule = 'preparer_differs_from_approver'),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT disclosure_approvals_separation CHECK (approved_by <> preparer_id),
    CONSTRAINT disclosure_approvals_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE UNIQUE INDEX disclosure_approvals_version_unique ON disclosure_approvals (version_id) WHERE NOT is_deleted;
CREATE INDEX disclosure_approvals_case_idx ON disclosure_approvals (case_id, approved_at, id) WHERE NOT is_deleted;

CREATE TRIGGER disclosure_approvals_set_updated_at BEFORE UPDATE ON disclosure_approvals FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER disclosure_approvals_append_only BEFORE UPDATE ON disclosure_approvals FOR EACH ROW EXECUTE FUNCTION reject_disclosure_content_change();

-- 7. An approval must match the version it approves: the same recipient, purpose and digest, and the preparer of that
--    version. The application checks this before it writes; the trigger keeps the rule even if a write bypasses it.
CREATE FUNCTION check_disclosure_approval_binding() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v RECORD;
BEGIN
    SELECT dv.package_id, dv.case_id, dv.recipient, dv.purpose, dv.digest, dv.created_by
    INTO v
    FROM disclosure_versions dv
    WHERE dv.id = NEW.version_id AND NOT dv.is_deleted;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'disclosure approval refers to an unknown version';
    END IF;
    IF (NEW.package_id, NEW.case_id, NEW.recipient, NEW.purpose, NEW.digest, NEW.preparer_id)
       IS DISTINCT FROM (v.package_id, v.case_id, v.recipient, v.purpose, v.digest, v.created_by) THEN
        RAISE EXCEPTION 'disclosure approval does not match the version it approves';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER disclosure_approvals_binding BEFORE INSERT ON disclosure_approvals
    FOR EACH ROW EXECUTE FUNCTION check_disclosure_approval_binding();
