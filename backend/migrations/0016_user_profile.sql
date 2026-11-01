-- Profile fields of a user, and an email that is required for every new account.
-- Existing rows keep a NULL email until an administrator or the user fills it in through the profile endpoints.
-- The application trims text, turns empty strings into NULL, and validates the email. The checks below are the backstop.

ALTER TABLE users
    ADD COLUMN first_name text,
    ADD COLUMN last_name text,
    ADD COLUMN title text,
    ADD COLUMN email text,
    ADD CONSTRAINT users_first_name_length CHECK (
        first_name IS NULL OR (char_length(first_name) BETWEEN 1 AND 100 AND first_name = btrim(first_name))
    ),
    ADD CONSTRAINT users_last_name_length CHECK (
        last_name IS NULL OR (char_length(last_name) BETWEEN 1 AND 100 AND last_name = btrim(last_name))
    ),
    ADD CONSTRAINT users_title_length CHECK (
        title IS NULL OR (char_length(title) BETWEEN 1 AND 100 AND title = btrim(title))
    ),
    ADD CONSTRAINT users_email_length CHECK (
        email IS NULL OR (char_length(email) BETWEEN 1 AND 254 AND email = btrim(email))
    );

-- Email addresses are unique among active accounts, ignoring case. A deleted account releases its address.
CREATE UNIQUE INDEX users_email_unique ON users (lower(email)) WHERE NOT is_deleted AND email IS NOT NULL;
