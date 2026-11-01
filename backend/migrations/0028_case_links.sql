-- Case links: two cases that concern the same incident are linked as a record only (owner decision, F1).
-- Linking changes nothing else: no task, deadline or status is merged or changed. Merging coordination is F2.
-- A pair is linked at most once, in either order: the unique index covers the unordered pair among non-deleted rows.
-- A link is removed by a soft delete, so the history stays complete. The application never deletes rows.
-- Every table keeps the standard columns and the updated_at trigger.
-- Grants come from the default privileges set up for the migrator role (db/init), so this migration adds none.

CREATE TABLE case_links (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES cases (id),
    linked_case_id uuid NOT NULL REFERENCES cases (id),
    created_by uuid NOT NULL REFERENCES users (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT case_links_distinct_cases CHECK (case_id <> linked_case_id),
    CONSTRAINT case_links_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

-- One active link per unordered pair of cases: (A, B) and (B, A) are the same link.
CREATE UNIQUE INDEX case_links_active_pair_unique ON case_links (
    LEAST(case_id, linked_case_id), GREATEST(case_id, linked_case_id)
) WHERE NOT is_deleted;

CREATE INDEX case_links_case_idx ON case_links (case_id) WHERE NOT is_deleted;
CREATE INDEX case_links_linked_case_idx ON case_links (linked_case_id) WHERE NOT is_deleted;

CREATE TRIGGER case_links_set_updated_at BEFORE UPDATE ON case_links FOR EACH ROW EXECUTE FUNCTION set_updated_at();
