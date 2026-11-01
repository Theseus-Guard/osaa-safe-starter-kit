-- Case record, part 2: the classification of a case, kept as numbered versions (plans/01 R01, R03 and plans/02).
-- A version is never changed: an edit inserts the next version, so the history of the classification stays complete.
-- Missing values are NULL (or an empty trigger set) and are marked as missing by the preliminary report.
-- triggers holds the five compact trigger values of R01. Intent or a claimed simulation is not a field here, so it
-- cannot waive a duty. internal_incident_option needs a rationale.
-- Every table keeps the standard columns and the updated_at trigger; the application never deletes rows.
-- Grants come from the default privileges set up for the migrator role (db/init), so this migration adds none.

CREATE TABLE case_classifications (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES cases (id),
    version integer NOT NULL CHECK (version >= 1),
    triggers text[] NOT NULL DEFAULT '{}' CHECK (triggers <@ ARRAY[
        'unauthorized_access_or_change',
        'boundary_escape',
        'confidential_data_exposure',
        'out_of_scope_continuation',
        'harm_inaction_near_miss'
    ]::text[]),
    event_kind text CHECK (event_kind IS NULL OR event_kind IN ('incident', 'near_miss', 'blocked_attempt')),
    internal_incident_option boolean NOT NULL DEFAULT false,
    classification_rationale text CHECK (classification_rationale IS NULL OR char_length(classification_rationale) BETWEEN 1 AND 1000),
    responsible_operator text CHECK (responsible_operator IS NULL OR char_length(responsible_operator) BETWEEN 1 AND 1000),
    affected_versions text CHECK (affected_versions IS NULL OR char_length(affected_versions) BETWEEN 1 AND 1000),
    approved_scope text CHECK (approved_scope IS NULL OR char_length(approved_scope) BETWEEN 1 AND 1000),
    scope_permission_source text CHECK (scope_permission_source IS NULL OR char_length(scope_permission_source) BETWEEN 1 AND 1000),
    containment text CHECK (containment IS NULL OR char_length(containment) BETWEEN 1 AND 1000),
    evidence_gap_plan text CHECK (evidence_gap_plan IS NULL OR char_length(evidence_gap_plan) BETWEEN 1 AND 1000),
    next_update_due_at timestamptz,
    created_by uuid NOT NULL REFERENCES users (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT case_classifications_case_version_unique UNIQUE (case_id, version),
    CONSTRAINT case_classifications_internal_needs_rationale CHECK (
        NOT internal_incident_option OR classification_rationale IS NOT NULL
    ),
    CONSTRAINT case_classifications_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE INDEX case_classifications_case_version_idx ON case_classifications (case_id, version DESC) WHERE NOT is_deleted;

CREATE TRIGGER case_classifications_set_updated_at BEFORE UPDATE ON case_classifications FOR EACH ROW EXECUTE FUNCTION set_updated_at();
