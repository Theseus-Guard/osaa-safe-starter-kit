-- Conflicts, recusals and independent appeals on review rounds (plans/f0/api-draft.md section 2.3, T09).
-- Every table is append-only. A correction is a new row that names the row it supersedes (supersedes_id); the current
-- entry of a reviewer on a round is the newest row of that reviewer, by seq. A row is never edited. The only change a
-- row may take is the soft delete (is_deleted, deleted_at), enforced by a trigger, as audit_events does.
-- No party can block an appeal: nothing in these tables records an approval from the original reviewer or a stakeholder.
-- Every table keeps the standard columns; the application never deletes rows.

-- The append-only rule for the three tables: the content columns may not change, and a soft delete is one-way.
CREATE FUNCTION reject_review_entry_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (to_jsonb(NEW) - 'updated_at' - 'is_deleted' - 'deleted_at')
        IS DISTINCT FROM (to_jsonb(OLD) - 'updated_at' - 'is_deleted' - 'deleted_at') THEN
        RAISE EXCEPTION 'review entries are append-only: their content cannot be changed';
    END IF;
    -- The only allowed change is the soft delete: is_deleted goes from false to true.
    IF NOT (NOT OLD.is_deleted AND NEW.is_deleted) AND (
        NEW.is_deleted IS DISTINCT FROM OLD.is_deleted OR NEW.deleted_at IS DISTINCT FROM OLD.deleted_at
    ) THEN
        RAISE EXCEPTION 'review entries are append-only: a soft delete cannot be undone or re-dated';
    END IF;
    RETURN NEW;
END;
$$;

-- A declaration of conflict by a reviewer about their own conflict on one round. kind 'declared' states the conflict;
-- kind 'withdrawn' states that the reviewer no longer declares it. A correction of a declaration is a new 'declared'
-- row that supersedes the current one.
CREATE TABLE review_conflicts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- Insertion order. The newest row of a reviewer on a round is that reviewer's current entry.
    seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    round_id uuid NOT NULL REFERENCES review_rounds (id),
    -- The reviewer the declaration is about. A reviewer declares only their own conflict.
    reviewer_id uuid NOT NULL REFERENCES users (id),
    kind text NOT NULL CHECK (kind IN ('declared', 'withdrawn')),
    description text CHECK (description IS NULL OR char_length(description) BETWEEN 1 AND 1000),
    -- The row this one corrects. Null for the first entry of a reviewer on a round.
    supersedes_id uuid REFERENCES review_conflicts (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT review_conflicts_description_when_declared CHECK (kind <> 'declared' OR description IS NOT NULL),
    CONSTRAINT review_conflicts_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE INDEX review_conflicts_round_reviewer_idx ON review_conflicts (round_id, reviewer_id, seq) WHERE NOT is_deleted;

CREATE TRIGGER review_conflicts_set_updated_at BEFORE UPDATE ON review_conflicts FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER review_conflicts_append_only BEFORE UPDATE ON review_conflicts FOR EACH ROW EXECUTE FUNCTION reject_review_entry_change();

-- A withdrawal of a reviewer from one round, with the replacement the reviewer names. The reviewer is always the caller.
-- A correction (for example a different replacement) is a new row that supersedes the current one.
CREATE TABLE review_recusals (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    round_id uuid NOT NULL REFERENCES review_rounds (id),
    -- The reviewer who withdraws.
    reviewer_id uuid NOT NULL REFERENCES users (id),
    replacement_id uuid NOT NULL REFERENCES users (id),
    reason text CHECK (reason IS NULL OR char_length(reason) BETWEEN 1 AND 1000),
    supersedes_id uuid REFERENCES review_recusals (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT review_recusals_replacement_is_another_user CHECK (replacement_id <> reviewer_id),
    CONSTRAINT review_recusals_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE INDEX review_recusals_round_reviewer_idx ON review_recusals (round_id, reviewer_id, seq) WHERE NOT is_deleted;

CREATE TRIGGER review_recusals_set_updated_at BEFORE UPDATE ON review_recusals FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER review_recusals_append_only BEFORE UPDATE ON review_recusals FOR EACH ROW EXECUTE FUNCTION reject_review_entry_change();

-- An appeal against one round, filed by a user who can see the case, and assigned to an independent reviewer. The
-- product does not decide an appeal: the row records the filing and the reviewer, nothing more.
CREATE TABLE review_appeals (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    round_id uuid NOT NULL REFERENCES review_rounds (id),
    filed_by uuid NOT NULL REFERENCES users (id),
    grounds text NOT NULL CHECK (char_length(grounds) BETWEEN 1 AND 2000),
    independent_reviewer_id uuid NOT NULL REFERENCES users (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT review_appeals_reviewer_is_not_filer CHECK (independent_reviewer_id <> filed_by),
    CONSTRAINT review_appeals_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE INDEX review_appeals_round_idx ON review_appeals (round_id, seq) WHERE NOT is_deleted;

CREATE TRIGGER review_appeals_set_updated_at BEFORE UPDATE ON review_appeals FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER review_appeals_append_only BEFORE UPDATE ON review_appeals FOR EACH ROW EXECUTE FUNCTION reject_review_entry_change();
