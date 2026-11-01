-- Reporter identity of each case opened through the intake, kept apart from the case content.
-- reporter_id is the account that submitted the case. contact is an optional way for the independent reporting owner
-- to reach the reporter. Nothing in the application returns either value; see safe_kit.reporter_channel.
-- Rows are written for cases submitted after this migration. Earlier cases keep their creator in cases.created_by.
-- Every table keeps the standard columns; the application never deletes rows.

CREATE TABLE reporter_identities (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES cases (id),
    reporter_id uuid NOT NULL REFERENCES users (id),
    contact text CHECK (contact IS NULL OR char_length(contact) BETWEEN 1 AND 200),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT reporter_identities_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

-- One active identity per case.
CREATE UNIQUE INDEX reporter_identities_case_active_unique ON reporter_identities (case_id) WHERE NOT is_deleted;

CREATE TRIGGER reporter_identities_set_updated_at BEFORE UPDATE ON reporter_identities FOR EACH ROW EXECUTE FUNCTION set_updated_at();
