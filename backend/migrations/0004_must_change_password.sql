-- New accounts start with a temporary password and must change it at first login.
-- Every account created before this migration is also forced to change its password.
ALTER TABLE users ADD COLUMN must_change_password boolean NOT NULL DEFAULT true;
