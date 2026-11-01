-- The reporter contact permission. Reading the contact a reporter gave is a separate permission from every case
-- permission, and it is granted to no role by default. An administrator assigns it through the role-permission matrix.
-- The read itself is audited (see safe_kit.reporter_channel). Grants come from the default privileges set up for the
-- migrator role (db/init), so this migration adds none.

INSERT INTO permissions (name, description) VALUES
    ('reporter_contact:read', 'Read the contact a reporter gave for a case (audited); holders also see reporter-channel cases before assignment');
