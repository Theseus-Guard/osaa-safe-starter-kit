-- Role-to-permission mapping moves from code into the database, so administrators can change it at runtime.
-- Revoking a permission is a soft delete of its role_permissions row, like every other delete in the system.

CREATE TABLE permissions (
    name text PRIMARY KEY,
    description text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT permissions_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);
CREATE TRIGGER permissions_set_updated_at BEFORE UPDATE ON permissions FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE role_permissions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    role text NOT NULL CHECK (role IN (
        'reporter',
        'triager',
        'investigator',
        'reviewer',
        'disclosure_approver',
        'auditor',
        'admin'
    )),
    permission text NOT NULL REFERENCES permissions (name),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT role_permissions_unique UNIQUE (role, permission),
    CONSTRAINT role_permissions_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);
CREATE TRIGGER role_permissions_set_updated_at BEFORE UPDATE ON role_permissions FOR EACH ROW EXECUTE FUNCTION set_updated_at();

INSERT INTO permissions (name, description) VALUES
    ('cases:submit', 'Submit a new case report'),
    ('cases:read_own', 'Read the cases the user submitted'),
    ('cases:read_assigned', 'Read cases assigned to the user (case ACL applies)'),
    ('cases:triage', 'Triage and classify assigned cases'),
    ('evidence:write', 'Add evidence to assigned cases'),
    ('reviews:write', 'Record reviews on assigned cases'),
    ('disclosures:approve', 'Approve disclosure packages'),
    ('audit:read', 'Read the audit log'),
    ('users:manage', 'List users, change roles and status, delete users'),
    ('users:reset_password', 'Set temporary passwords for other users'),
    ('roles:manage', 'Change the permissions of roles');

INSERT INTO role_permissions (role, permission) VALUES
    ('reporter', 'cases:submit'),
    ('reporter', 'cases:read_own'),
    ('triager', 'cases:read_assigned'),
    ('triager', 'cases:triage'),
    ('investigator', 'cases:read_assigned'),
    ('investigator', 'evidence:write'),
    ('reviewer', 'cases:read_assigned'),
    ('reviewer', 'reviews:write'),
    ('disclosure_approver', 'disclosures:approve'),
    ('auditor', 'audit:read'),
    ('admin', 'users:manage'),
    ('admin', 'users:reset_password'),
    ('admin', 'roles:manage');
