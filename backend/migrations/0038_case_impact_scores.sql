-- Case risk: the impact values of a case, C (Confidentiality), B (Integrity) and A (Availability), kept as numbered
-- versions. Each value is an integer from 1 to 5, or NULL when it is not set yet. A case with no version has no values.
-- The risk score is not stored: it is computed from the stored values (safe_kit.impact.risk_score).
-- A version is never changed: an edit inserts the next version, as the classification does (migration 0024).
-- Every table keeps the standard columns and the updated_at trigger; the application never deletes rows.
-- Grants come from the default privileges set up for the migrator role (db/init), so this migration adds none.

CREATE TABLE case_impact_scores (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES cases (id),
    version integer NOT NULL CHECK (version >= 1),
    confidentiality integer CHECK (confidentiality IS NULL OR confidentiality BETWEEN 1 AND 5),
    integrity integer CHECK (integrity IS NULL OR integrity BETWEEN 1 AND 5),
    availability integer CHECK (availability IS NULL OR availability BETWEEN 1 AND 5),
    created_by uuid NOT NULL REFERENCES users (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT case_impact_scores_case_version_unique UNIQUE (case_id, version),
    CONSTRAINT case_impact_scores_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE INDEX case_impact_scores_case_version_idx ON case_impact_scores (case_id, version DESC) WHERE NOT is_deleted;

CREATE TRIGGER case_impact_scores_set_updated_at BEFORE UPDATE ON case_impact_scores FOR EACH ROW EXECUTE FUNCTION set_updated_at();
