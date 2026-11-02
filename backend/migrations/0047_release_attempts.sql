-- Release attempts of a disclosure version (F2, T06-B; plans/03 T06-B, plans/02 release and execution rules).
--
-- A release is a notice record (safe_kit.external_notices) or a disclosure export (safe_kit.disclosure.export_version).
-- Each one writes a release attempt before the release check runs, in the same transaction. The check requires an open
-- attempt for the version and purpose: an 'attempted' row with no outcome row yet. The same transaction then writes the
-- outcome row: 'done' when the release goes through, or 'refused' (with a reason code) when the check or a later rule
-- refuses it. A refused attempt is kept as history and never counts as a release.
--
-- Rows are insert-only: the trigger reject_disclosure_content_change (migration 0040) rejects any content change. An
-- attempt has one 'attempted' row whose attempt_id is its own id, and at most one outcome row that points to it.

CREATE TABLE release_attempts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- The attempt this row belongs to. An 'attempted' row points to itself.
    attempt_id uuid NOT NULL REFERENCES release_attempts (id),
    state text NOT NULL CHECK (state IN ('attempted', 'done', 'refused')),
    purpose text NOT NULL CHECK (purpose IN ('external_notice', 'export')),
    case_id uuid NOT NULL REFERENCES cases (id),
    package_id uuid NOT NULL REFERENCES disclosure_packages (id),
    version_id uuid NOT NULL REFERENCES disclosure_versions (id),
    actor_id uuid NOT NULL REFERENCES users (id),
    -- When this row was written: the start of the attempt, or its outcome.
    recorded_at timestamptz NOT NULL DEFAULT now(),
    -- Why a refused attempt was refused: the release check outcome or the rule that refused it.
    reason_code text CHECK (char_length(reason_code) BETWEEN 1 AND 64),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT release_attempts_start_points_to_itself CHECK ((state = 'attempted') = (attempt_id = id)),
    CONSTRAINT release_attempts_refused_has_reason CHECK ((state = 'refused') = (reason_code IS NOT NULL)),
    CONSTRAINT release_attempts_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

-- One outcome per attempt.
CREATE UNIQUE INDEX release_attempts_outcome_unique ON release_attempts (attempt_id) WHERE state <> 'attempted' AND NOT is_deleted;
CREATE INDEX release_attempts_version_idx ON release_attempts (version_id, recorded_at, id) WHERE NOT is_deleted;

CREATE TRIGGER release_attempts_set_updated_at BEFORE UPDATE ON release_attempts FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER release_attempts_append_only BEFORE UPDATE ON release_attempts FOR EACH ROW EXECUTE FUNCTION reject_disclosure_content_change();

-- An outcome row must close an open attempt of the same version and purpose. The application writes it in the same
-- transaction; the trigger keeps the rule even if a write bypasses the application.
CREATE FUNCTION check_release_attempt_outcome() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.state = 'attempted' THEN
        RETURN NEW;
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM release_attempts a
        WHERE a.id = NEW.attempt_id AND a.state = 'attempted' AND NOT a.is_deleted
          AND a.version_id = NEW.version_id AND a.purpose = NEW.purpose
    ) THEN
        RAISE EXCEPTION 'release outcome needs its open attempt for the same version and purpose';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER release_attempts_outcome_check BEFORE INSERT ON release_attempts
    FOR EACH ROW EXECUTE FUNCTION check_release_attempt_outcome();
