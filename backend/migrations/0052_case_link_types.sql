-- Typed case links (F3 A9, owner decision 2026-11-03).
-- Every new link has a type, a reason and a visibility choice (default off). A duplicate or root_sub link names its
-- main or root case in primary_case_id; a related link has none.
-- Links made before this migration get type 'related' from the column default: they only ever gave a reference, which
-- is what related means. They have no reason, so the reason check accepts NULL. Their reason stays NULL.
-- Additive only: columns and constraints are added. No row is removed or rewritten.
-- Grants come from the default privileges set up for the migrator role (db/init), so this migration adds none.

ALTER TABLE case_links
    ADD COLUMN link_type text NOT NULL DEFAULT 'related',
    ADD COLUMN reason text,
    ADD COLUMN primary_case_id uuid REFERENCES cases (id),
    ADD COLUMN share_visibility boolean NOT NULL DEFAULT false;

ALTER TABLE case_links ALTER COLUMN link_type DROP DEFAULT;

ALTER TABLE case_links ADD CONSTRAINT case_links_type_check
    CHECK (link_type IN ('duplicate', 'related', 'root_sub'));

-- A related link has no main case. A duplicate or root_sub link has one, and it is one of the two cases.
ALTER TABLE case_links ADD CONSTRAINT case_links_primary_check CHECK (
    (link_type = 'related') = (primary_case_id IS NULL)
    AND (primary_case_id IS NULL OR primary_case_id IN (case_id, linked_case_id))
);

-- The application requires a reason on every new link. The check keeps the stored text short and never blank; it
-- accepts NULL so that links made before this migration can still be soft deleted.
ALTER TABLE case_links ADD CONSTRAINT case_links_reason_check CHECK (
    reason IS NULL OR (btrim(reason) <> '' AND char_length(reason) <= 500)
);

CREATE INDEX case_links_primary_idx ON case_links (primary_case_id)
    WHERE NOT is_deleted AND primary_case_id IS NOT NULL;
