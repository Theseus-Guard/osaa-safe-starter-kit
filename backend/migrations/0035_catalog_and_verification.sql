-- Suggestions, verification runs, and the versioned catalog (plans/f0/api-draft.md section 2.6, R10 and R11).
--
-- A recommendation is a suggestion: a failure or affected system, the defence outcome it needs, a minimum control, an
-- owner, a due time, and the evidence to keep. It has no result field, so it can never be stored as verified.
-- A verification run is a separate record that checked a recommendation: what was checked, its scope, assumptions,
-- evidence path, failure behaviour, error class, uncertainty, method, result, and who checked it and when.
-- A catalog entry is an identity. Its content lives in catalog_versions, which is insert-only: every operation
-- (create, version, correction, dispute, withdrawal, supersession) appends the next version and never edits one.
-- A withdrawn or superseded version keeps its history and, for a supersession, points to its replacement entry.
-- A hidden reference (for example a secret report link) and its digest live in catalog_restricted_references, apart
-- from the public columns, so a public derivative of the catalog never reads them.
--
-- The four tables that hold content (recommendations, verification runs, versions, restricted references) are
-- insert-only: a trigger rejects any change to their content. Only a soft delete is allowed, and the application never
-- deletes rows.
-- Permissions catalog:read, catalog:write, and catalog:read_restricted are seeded here, with the default grants.

CREATE TABLE catalog_recommendations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    failure_or_system text NOT NULL CHECK (char_length(failure_or_system) BETWEEN 1 AND 1000),
    required_defence_outcome text NOT NULL CHECK (char_length(required_defence_outcome) BETWEEN 1 AND 1000),
    minimum_control text NOT NULL CHECK (char_length(minimum_control) BETWEEN 1 AND 1000),
    control_alternative text CHECK (control_alternative IS NULL OR char_length(control_alternative) BETWEEN 1 AND 1000),
    responsible text NOT NULL CHECK (char_length(responsible) BETWEEN 1 AND 1000),
    due_at timestamptz NOT NULL,
    evidence_to_keep text NOT NULL CHECK (char_length(evidence_to_keep) BETWEEN 1 AND 1000),
    created_by uuid NOT NULL REFERENCES users (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT catalog_recommendations_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE INDEX catalog_recommendations_created_idx ON catalog_recommendations (created_at, id) WHERE NOT is_deleted;

CREATE TRIGGER catalog_recommendations_set_updated_at BEFORE UPDATE ON catalog_recommendations FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE catalog_verification_runs (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    recommendation_id uuid NOT NULL REFERENCES catalog_recommendations (id),
    subject text NOT NULL CHECK (char_length(subject) BETWEEN 1 AND 1000),
    scope text NOT NULL CHECK (char_length(scope) BETWEEN 1 AND 1000),
    assumptions text NOT NULL CHECK (char_length(assumptions) BETWEEN 1 AND 1000),
    evidence_path text NOT NULL CHECK (char_length(evidence_path) BETWEEN 1 AND 1000),
    failure_behavior text NOT NULL CHECK (char_length(failure_behavior) BETWEEN 1 AND 1000),
    error_class text NOT NULL CHECK (char_length(error_class) BETWEEN 1 AND 1000),
    uncertainty text NOT NULL CHECK (char_length(uncertainty) BETWEEN 1 AND 1000),
    method text NOT NULL CHECK (char_length(method) BETWEEN 1 AND 1000),
    result text NOT NULL CHECK (result IN ('pass', 'fail', 'inconclusive')),
    verified_by uuid NOT NULL REFERENCES users (id),
    verified_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT catalog_verification_runs_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE INDEX catalog_verification_runs_recommendation_idx ON catalog_verification_runs (recommendation_id, verified_at, id) WHERE NOT is_deleted;

CREATE TRIGGER catalog_verification_runs_set_updated_at BEFORE UPDATE ON catalog_verification_runs FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE catalog_entries (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    created_by uuid NOT NULL REFERENCES users (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT catalog_entries_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE INDEX catalog_entries_created_idx ON catalog_entries (created_at, id) WHERE NOT is_deleted;

CREATE TRIGGER catalog_entries_set_updated_at BEFORE UPDATE ON catalog_entries FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE catalog_versions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    entry_id uuid NOT NULL REFERENCES catalog_entries (id),
    -- Starts at 1 and grows by one for every operation on the entry.
    version integer NOT NULL CHECK (version >= 1),
    operation text NOT NULL CHECK (operation IN ('create', 'version', 'correction', 'dispute', 'withdrawal', 'supersession')),
    -- The state of the entry after this version. Only the matching operation sets a terminal or disputed state.
    status text NOT NULL CHECK (status IN ('active', 'disputed', 'withdrawn', 'superseded')),
    title text NOT NULL CHECK (char_length(title) BETWEEN 1 AND 200),
    body text NOT NULL CHECK (char_length(body) BETWEEN 1 AND 4000),
    -- Required for a correction, a dispute, and a withdrawal. Optional for a supersession.
    reason text CHECK (reason IS NULL OR char_length(reason) BETWEEN 1 AND 1000),
    -- Set only by a supersession: the entry that replaces this one.
    replacement_entry_id uuid REFERENCES catalog_entries (id),
    -- The verification run this content rests on, if any. A recommendation alone never sets it.
    verification_run_id uuid REFERENCES catalog_verification_runs (id),
    created_by uuid NOT NULL REFERENCES users (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT catalog_versions_entry_version_unique UNIQUE (entry_id, version),
    CONSTRAINT catalog_versions_create_is_first CHECK ((operation = 'create') = (version = 1)),
    CONSTRAINT catalog_versions_status_matches_operation CHECK (
        (status = 'withdrawn') = (operation = 'withdrawal')
        AND (status = 'disputed') = (operation = 'dispute')
        AND (status = 'superseded') = (operation = 'supersession')
        AND (status = 'active') = (operation IN ('create', 'version', 'correction'))
    ),
    CONSTRAINT catalog_versions_reason_required CHECK (
        operation NOT IN ('correction', 'dispute', 'withdrawal') OR reason IS NOT NULL
    ),
    CONSTRAINT catalog_versions_replacement_only_for_supersession CHECK (
        (operation = 'supersession') = (replacement_entry_id IS NOT NULL)
    ),
    CONSTRAINT catalog_versions_replacement_is_other_entry CHECK (
        replacement_entry_id IS NULL OR replacement_entry_id <> entry_id
    ),
    CONSTRAINT catalog_versions_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE INDEX catalog_versions_entry_version_idx ON catalog_versions (entry_id, version DESC) WHERE NOT is_deleted;

CREATE TRIGGER catalog_versions_set_updated_at BEFORE UPDATE ON catalog_versions FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE catalog_restricted_references (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    version_id uuid NOT NULL REFERENCES catalog_versions (id),
    -- A hidden reference, for example a link to a restricted report. Returned only to catalog:read_restricted.
    reference text NOT NULL CHECK (char_length(reference) BETWEEN 1 AND 2000),
    -- SHA-256 of the reference, lower-case hex. Internal integrity digest; never part of a public derivative.
    digest text NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
    created_by uuid NOT NULL REFERENCES users (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT catalog_restricted_references_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE UNIQUE INDEX catalog_restricted_references_version_unique ON catalog_restricted_references (version_id) WHERE NOT is_deleted;

CREATE TRIGGER catalog_restricted_references_set_updated_at BEFORE UPDATE ON catalog_restricted_references FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Insert-only content: an update may change only the soft-delete columns and updated_at.
CREATE FUNCTION reject_catalog_content_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (to_jsonb(NEW) - ARRAY['updated_at', 'is_deleted', 'deleted_at']) IS DISTINCT FROM (to_jsonb(OLD) - ARRAY['updated_at', 'is_deleted', 'deleted_at']) THEN
        RAISE EXCEPTION 'catalog records are append-only: their content cannot be changed';
    END IF;
    -- The only allowed change is the soft delete: is_deleted goes from false to true.
    IF NOT (NOT OLD.is_deleted AND NEW.is_deleted) AND (
        NEW.is_deleted IS DISTINCT FROM OLD.is_deleted OR NEW.deleted_at IS DISTINCT FROM OLD.deleted_at
    ) THEN
        RAISE EXCEPTION 'catalog records are append-only: a soft delete cannot be undone or re-dated';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER catalog_recommendations_append_only BEFORE UPDATE ON catalog_recommendations FOR EACH ROW EXECUTE FUNCTION reject_catalog_content_change();
CREATE TRIGGER catalog_verification_runs_append_only BEFORE UPDATE ON catalog_verification_runs FOR EACH ROW EXECUTE FUNCTION reject_catalog_content_change();
CREATE TRIGGER catalog_versions_append_only BEFORE UPDATE ON catalog_versions FOR EACH ROW EXECUTE FUNCTION reject_catalog_content_change();
CREATE TRIGGER catalog_restricted_references_append_only BEFORE UPDATE ON catalog_restricted_references FOR EACH ROW EXECUTE FUNCTION reject_catalog_content_change();

-- Permissions. catalog:read reads the internal view (login required). catalog:write records suggestions,
-- verification runs, and catalog versions. catalog:read_restricted shows restricted references and their digests.
-- The inserts do nothing when the row already exists. Forward only.
INSERT INTO permissions (name, description) VALUES
    ('catalog:read', 'Read the catalog entries, their versions, and verification results'),
    ('catalog:write', 'Record recommendations, verification runs, and catalog versions'),
    ('catalog:read_restricted', 'See restricted catalog references and their digests')
ON CONFLICT (name) DO NOTHING;

INSERT INTO role_permissions (role, permission) VALUES
    ('triager', 'catalog:read'),
    ('investigator', 'catalog:read'),
    ('reviewer', 'catalog:read'),
    ('reviewer', 'catalog:write'),
    ('auditor', 'catalog:read')
ON CONFLICT (role, permission) DO NOTHING;
