-- Tokens issued before this timestamp are rejected (set on password reset and account disable).
ALTER TABLE users ADD COLUMN sessions_valid_after timestamptz NOT NULL DEFAULT to_timestamp(0);

-- Opaque refresh tokens. Only a SHA-256 hash is stored. Rotated tokens keep their family_id.
CREATE TABLE refresh_tokens (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id uuid NOT NULL REFERENCES users (id),
    family_id uuid NOT NULL,
    token_hash text NOT NULL UNIQUE,
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX refresh_tokens_family_idx ON refresh_tokens (family_id);

-- Access tokens revoked by logout. Rows may be deleted once expires_at has passed.
CREATE TABLE revoked_access_tokens (
    jti text PRIMARY KEY,
    expires_at timestamptz NOT NULL
);

-- Fixed-window counters for rate limiting.
CREATE TABLE rate_limits (
    bucket text PRIMARY KEY,
    window_start timestamptz NOT NULL,
    hits integer NOT NULL
);
