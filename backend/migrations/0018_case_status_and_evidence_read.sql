-- Reasons for closing and reopening a case, stored as text, and the evidence:read permission.
-- Each close or reopen writes one row here in the same transaction as the status change. The audit row keeps
-- only the length of the reason, as before. The case history reads the reason from this table for callers
-- with cases:read_all. Grants come from the default privileges set up for the migrator role (db/init).
-- evidence:read gates listing, downloading, and the chain of custody of evidence. Uploads keep evidence:write.
-- Every table keeps the standard columns; the application never deletes rows.

CREATE TABLE case_status_changes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES cases (id),
    action text NOT NULL CHECK (action IN ('closed', 'reopened')),
    reason text NOT NULL CHECK (char_length(reason) BETWEEN 1 AND 1000),
    actor_id uuid NOT NULL REFERENCES users (id),
    changed_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT case_status_changes_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE INDEX case_status_changes_case_idx ON case_status_changes (case_id, changed_at) WHERE NOT is_deleted;

CREATE TRIGGER case_status_changes_set_updated_at BEFORE UPDATE ON case_status_changes FOR EACH ROW EXECUTE FUNCTION set_updated_at();

INSERT INTO permissions (name, description) VALUES
    ('evidence:read', 'Read evidence of visible cases: list, download, and chain of custody');

INSERT INTO role_permissions (role, permission) VALUES
    ('investigator', 'evidence:read'),
    ('reviewer', 'evidence:read'),
    ('triager', 'evidence:read');
