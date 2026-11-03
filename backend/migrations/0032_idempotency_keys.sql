-- Idempotency-Key records for side-effect POST requests (plans/f0/api-draft.md section 3.4).
-- The scope of a key is (principal, method, route template, key). The row keeps the request fingerprint (SHA-256 of
-- the normalised method, path, query, and body) and the stored response, so a repeated request gets the same answer.
-- The row is written in the same transaction as the domain change, so a rolled-back request leaves no key behind.
-- A key older than the replay window is soft-deleted by the application and the request counts as new.
-- Every table keeps the standard columns; the application never deletes rows.

CREATE TABLE idempotency_keys (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    principal_id uuid NOT NULL REFERENCES users (id),
    method text NOT NULL CHECK (method IN ('POST')),
    route text NOT NULL CHECK (char_length(route) BETWEEN 1 AND 200),
    key text NOT NULL CHECK (key ~ '^[!-~]{1,128}$'),
    fingerprint text NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    response_status integer NOT NULL CHECK (response_status BETWEEN 200 AND 599),
    response_body jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT idempotency_keys_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

-- One live record per scope. A soft-deleted (expired) record does not block a new one with the same key.
CREATE UNIQUE INDEX idempotency_keys_scope_unique ON idempotency_keys (principal_id, method, route, key) WHERE NOT is_deleted;

CREATE TRIGGER idempotency_keys_set_updated_at BEFORE UPDATE ON idempotency_keys FOR EACH ROW EXECUTE FUNCTION set_updated_at();
