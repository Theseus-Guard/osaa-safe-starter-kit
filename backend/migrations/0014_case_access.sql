-- Case access control, investigator and reviewer assignment, reviews, closure and reopening, and deadline status.
-- A case is visible to its reporter (cases:read_own), to every caller with cases:read_all (triagers), and to
-- callers with cases:read_assigned who hold an active assignment. Everything else is answered as not found.
-- Closing needs at least one review and every deadline task done or explicitly not applicable.
-- Every table keeps the standard columns; the application never deletes rows.

INSERT INTO permissions (name, description) VALUES
    ('cases:read_all', 'Read every case, not only the own or assigned ones'),
    ('cases:assign', 'Assign and unassign investigators and reviewers on a case'),
    ('cases:close', 'Close a case and reopen a closed case');

INSERT INTO role_permissions (role, permission) VALUES
    ('triager', 'cases:read_all'),
    ('triager', 'cases:assign'),
    ('triager', 'cases:close');

ALTER TABLE cases
    ADD COLUMN closed_at timestamptz,
    ADD COLUMN closed_by uuid REFERENCES users (id),
    ADD COLUMN close_reason text CHECK (close_reason IS NULL OR char_length(close_reason) BETWEEN 1 AND 1000),
    ADD CONSTRAINT cases_closed_consistent CHECK (
        (status = 'closed' AND closed_at IS NOT NULL AND closed_by IS NOT NULL AND close_reason IS NOT NULL)
        OR (status = 'open' AND closed_at IS NULL AND closed_by IS NULL)
    );

CREATE TABLE case_assignments (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES cases (id),
    user_id uuid NOT NULL REFERENCES users (id),
    assigned_by uuid NOT NULL REFERENCES users (id),
    assigned_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT case_assignments_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

-- One active assignment per user and case. A removed assignment is a soft delete, so the user can be assigned again.
CREATE UNIQUE INDEX case_assignments_active_unique ON case_assignments (case_id, user_id) WHERE NOT is_deleted;
CREATE INDEX case_assignments_user_idx ON case_assignments (user_id, case_id) WHERE NOT is_deleted;

CREATE TRIGGER case_assignments_set_updated_at BEFORE UPDATE ON case_assignments FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE case_reviews (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES cases (id),
    reviewer_id uuid NOT NULL REFERENCES users (id),
    outcome text NOT NULL CHECK (outcome IN ('pass', 'fail', 'insufficient_evidence', 'not_applicable')),
    note text CHECK (note IS NULL OR char_length(note) BETWEEN 1 AND 1000),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT case_reviews_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE INDEX case_reviews_case_idx ON case_reviews (case_id) WHERE NOT is_deleted;

CREATE TRIGGER case_reviews_set_updated_at BEFORE UPDATE ON case_reviews FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- Case history reads audit rows by their case target. Without this index the history query scans the whole log.
CREATE INDEX audit_events_case_target_idx ON audit_events (target_id, created_at DESC, id) WHERE NOT is_deleted AND target_type = 'case';

ALTER TABLE deadline_tasks DROP CONSTRAINT deadline_tasks_status_check;
ALTER TABLE deadline_tasks
    ADD CONSTRAINT deadline_tasks_status_check CHECK (status IN ('open', 'done', 'not_applicable')),
    ADD COLUMN not_applicable_reason text,
    ADD CONSTRAINT deadline_tasks_not_applicable_reason_check CHECK (
        (status = 'not_applicable') = (not_applicable_reason IS NOT NULL)
        AND (not_applicable_reason IS NULL OR char_length(not_applicable_reason) BETWEEN 1 AND 1000)
    );
