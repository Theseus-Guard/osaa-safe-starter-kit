-- The institution_owner role (F3, owner decisions 2026-11-03; product manager recommendation applied).
--
-- Assigning the backup approvers of a case, and changing the waiting time of a backup, belong to the institution owner
-- only. Migration 0054 added disclosures:assign_backup and granted it to no role. This migration:
--
-- 1. adds the role institution_owner wherever the database enforces role names (users.role and role_permissions.role),
--    replacing the two CHECK constraints the way migration 0029 did;
-- 2. adds the permission disclosures:set_backup_wait, which changes the waiting time (migration 0057 keeps its history);
-- 3. grants the owner permissions to institution_owner and to no other role:
--    - disclosures:assign_backup and disclosures:set_backup_wait (the owner's two rights);
--    - disclosures:read, to list the backup pool and the notice settings;
--    - cases:read_all, so the owner can see the case whose backup pool it assigns. Without it the owner could not reach
--      the case at all. The owner holds no approval, prepare, triage or evidence permission.
--
-- The administrator and the disclosure approver do not get the owner permissions. The role matrix (safe_kit.rbac) refuses
-- these two permissions for every other role, and the user administration refuses to set or change the role
-- (safe_kit.api, admin user routes).
--
-- Forward only. The inserts do nothing when the row already exists, so the migration is safe on a database where an
-- administrator created the role or grant by hand.

ALTER TABLE users DROP CONSTRAINT users_role_check;
ALTER TABLE users ADD CONSTRAINT users_role_check CHECK (role IN (
    'reporter',
    'triager',
    'investigator',
    'reviewer',
    'disclosure_approver',
    'auditor',
    'admin',
    'reporting_owner',
    'institution_owner'
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
    'reporting_owner',
    'institution_owner'
));

INSERT INTO permissions (name, description) VALUES
    ('disclosures:set_backup_wait', 'Change the waiting time before a backup may act on an external notice (institution owner only, with a reason)')
ON CONFLICT (name) DO NOTHING;

INSERT INTO role_permissions (role, permission) VALUES
    ('institution_owner', 'disclosures:assign_backup'),
    ('institution_owner', 'disclosures:set_backup_wait'),
    ('institution_owner', 'disclosures:read'),
    ('institution_owner', 'cases:read_all')
ON CONFLICT (role, permission) DO NOTHING;
