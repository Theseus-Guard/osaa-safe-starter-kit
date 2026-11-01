-- Personal data inventory is readable by administrators only, and it is metadata only.
INSERT INTO permissions (name, description) VALUES
    ('privacy:read', 'Read the personal data inventory and its export (metadata only)');

INSERT INTO role_permissions (role, permission) VALUES ('admin', 'privacy:read');
