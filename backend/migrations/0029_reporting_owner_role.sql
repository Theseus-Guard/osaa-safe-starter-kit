-- The reporting_owner role (owner decision, F1). It is the one role that reads the contact a reporter gave.
-- Forward only. No other role is changed.
--
-- The role is added wherever the database enforces role names: the users.role and role_permissions.role CHECK
-- constraints. Both were created inline in 0002 and 0007, so PostgreSQL named them users_role_check and
-- role_permissions_role_check, and they are replaced here.
--
-- Two grants are seeded, and the role matrix cannot change them (safe_kit.rbac, FIXED_REPORTER_CONTACT_GRANT):
-- - reporter_contact:read, the contact permission. The contact route checks it.
-- - cases:read_all. The contact route also checks case visibility, and a reporter-channel case with no assignee is
--   visible only to a read_all caller (cases.CaseScope.visibility_sql). cases:read_own and cases:read_assigned cannot
--   reach such a case: the reporter account created it, and a reporting_owner cannot be assigned because it does not
--   hold cases:assignable. The role holds no triage, assign, evidence or review permission.
--
-- The inserts do nothing when the row already exists, so the migration is safe on a database where an administrator
-- created the role or grant by hand.

ALTER TABLE users DROP CONSTRAINT users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN (
    'reporter',
    'triager',
    'investigator',
    'reviewer',
    'disclosure_approver',
    'auditor',
    'admin',
    'reporting_owner'
));

ALTER TABLE role_permissions DROP CONSTRAINT role_permissions_role_check;
ALTER TABLE role_permissions ADD CONSTRAINT role_permissions_role_check CHECK (role IN (
    'reporter',
    'triager',
    'investigator',
    'reviewer',
    'disclosure_approver',
    'auditor',
    'admin',
    'reporting_owner'
));

INSERT INTO role_permissions (role, permission) VALUES
    ('reporting_owner', 'reporter_contact:read'),
    ('reporting_owner', 'cases:read_all')
ON CONFLICT (role, permission) DO NOTHING;
