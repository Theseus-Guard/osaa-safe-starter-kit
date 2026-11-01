-- Temporary passwords are never accepted as a new password, however long ago they were issued.
-- is_temporary marks a password_history row whose password was issued by an admin or the operator:
-- account creation, admin reset, and the bootstrap admin. The application writes it in the same
-- transaction as the history row. A new password may not match any temporary row of the account.
ALTER TABLE password_history
    ADD COLUMN is_temporary boolean NOT NULL DEFAULT false;

-- Only the current password of an account is known here. An account whose current password is still a
-- forced change holds a temporary password, so that row is marked. Earlier rows of a database that
-- already recorded password changes under 0019 cannot be classified and keep the default.
UPDATE password_history AS history
SET is_temporary = true
FROM users
WHERE users.id = history.user_id
  AND users.password_hash = history.password_hash
  AND users.must_change_password;
