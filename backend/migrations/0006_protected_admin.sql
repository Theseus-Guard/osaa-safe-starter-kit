-- The first admin created by the bootstrap is protected: it can never be disabled, deleted, or demoted.
-- Only one account can be protected. The application checks this too; the trigger is the backstop.
ALTER TABLE users ADD COLUMN is_protected boolean NOT NULL DEFAULT false;

CREATE UNIQUE INDEX users_single_protected ON users ((true)) WHERE is_protected;

-- Existing installations: the earliest admin becomes the protected admin.
UPDATE users
SET is_protected = true
WHERE id = (SELECT id FROM users WHERE role = 'admin' ORDER BY created_at, id LIMIT 1);

CREATE FUNCTION protect_admin_account() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.is_protected AND (
        NOT NEW.is_protected OR NEW.is_deleted OR NOT NEW.is_active OR NEW.role <> 'admin'
    ) THEN
        RAISE EXCEPTION 'protected admin account cannot be disabled, deleted, or demoted';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER users_protect_admin BEFORE UPDATE ON users FOR EACH ROW EXECUTE FUNCTION protect_admin_account();
