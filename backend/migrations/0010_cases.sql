-- Minimal case domain: a case is submitted by a reporter with the awareness time t0 (reasonable suspicion, see plans/01).
-- Deadline tasks are computed from t0 when the case is created. Only the creator can read a case for now;
-- assignment, evidence, reviews, and disclosures are separate work.

CREATE SEQUENCE case_reference_seq;

CREATE TABLE cases (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    reference text NOT NULL UNIQUE,
    title text NOT NULL,
    awareness_time timestamptz NOT NULL,
    created_by uuid NOT NULL REFERENCES users (id),
    status text NOT NULL CHECK (status IN ('open', 'closed')),
    calendar_version text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT cases_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE INDEX cases_created_by_idx ON cases (created_by, created_at DESC, id) WHERE NOT is_deleted;

CREATE TRIGGER cases_set_updated_at BEFORE UPDATE ON cases FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE deadline_tasks (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES cases (id),
    kind text NOT NULL CHECK (kind IN (
        'affected_customers',
        'preliminary_safe_report',
        'broader_advisory',
        'preliminary_public_report',
        'remediation_status',
        'weekly_update'
    )),
    due_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('open', 'done')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT deadline_tasks_case_kind_unique UNIQUE (case_id, kind),
    CONSTRAINT deadline_tasks_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE INDEX deadline_tasks_case_idx ON deadline_tasks (case_id) WHERE NOT is_deleted;

CREATE TRIGGER deadline_tasks_set_updated_at BEFORE UPDATE ON deadline_tasks FOR EACH ROW EXECUTE FUNCTION set_updated_at();
