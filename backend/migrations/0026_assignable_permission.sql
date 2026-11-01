-- Assignability is a permission of its own. A user may be assigned to a case only when its role holds cases:assignable,
-- and the assignee picker lists only such users. cases:assign (who makes assignments) stays with the triager role.
-- Investigator and reviewer hold cases:assignable by default. An administrator changes these grants later through
-- the role-permission matrix. The inserts do nothing when the row already exists, so the migration is safe on a
-- database where an administrator created the permission or grant by hand. Forward only.

INSERT INTO permissions (name, description) VALUES
    ('cases:assignable', 'Can be assigned to a case and listed in the assignee picker (investigator and reviewer by default)')
ON CONFLICT (name) DO NOTHING;

INSERT INTO role_permissions (role, permission) VALUES
    ('investigator', 'cases:assignable'),
    ('reviewer', 'cases:assignable')
ON CONFLICT (role, permission) DO NOTHING;
