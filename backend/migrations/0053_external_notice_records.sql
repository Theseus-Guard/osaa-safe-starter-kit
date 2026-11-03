-- The T12-A records of an external notice (F3, work package A8; plans/03 T12-A, plans/02 disclosure flow, plans/01 roles 5
-- and 2). Three kinds of record, each about one approved or prepared disclosure version:
--
-- 1. external_notice_escalations: an escalation. The named approver cannot record an urgent notice (unreachable, or in a
--    conflict of interest), so an independent authorised backup is named, with the authority basis, the missing facts and
--    the next update. Nothing here changes a deadline task: the deadline keeps running.
-- 2. external_notice_reviews with review_kind 'recipient': a review of the selected recipient, bound to the version digest.
-- 3. external_notice_reviews with review_kind 'package': a review of the package, bound to the version digest.
--
-- The rows are insert-only: the trigger reject_disclosure_content_change (migration 0040) rejects any content change, and
-- the application never deletes rows. Free text is kept in the rows and never written to the audit log.
--
-- The rules the application checks before it writes (safe_kit.external_notice_records): the case is open and visible,
-- the version belongs to the case, a preparer may not name themselves as backup or review their own version, the backup
-- holds disclosures:approve. The database keeps the shape of each row and the insert-only rule.

-- 1. Escalations of a notice that the named approver cannot send.
CREATE TABLE external_notice_escalations (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES cases (id),
    package_id uuid NOT NULL REFERENCES disclosure_packages (id),
    version_id uuid NOT NULL REFERENCES disclosure_versions (id),
    -- Why the named approver cannot act: not reachable, or in a conflict of interest.
    reason_code text NOT NULL CHECK (reason_code IN ('unreachable', 'conflict_of_interest')),
    -- The authority under which the backup acts, the facts still missing, and when the next update follows.
    authority_basis text NOT NULL CHECK (char_length(authority_basis) BETWEEN 1 AND 500),
    missing_facts text NOT NULL CHECK (char_length(missing_facts) BETWEEN 1 AND 500),
    next_update text NOT NULL CHECK (char_length(next_update) BETWEEN 1 AND 500),
    -- The independent authorised backup (a holder of disclosures:approve who did not prepare the version).
    backup_user_id uuid NOT NULL REFERENCES users (id),
    escalated_by uuid NOT NULL REFERENCES users (id),
    escalated_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT external_notice_escalations_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE INDEX external_notice_escalations_version_idx ON external_notice_escalations (version_id, escalated_at, id) WHERE NOT is_deleted;
CREATE INDEX external_notice_escalations_case_idx ON external_notice_escalations (case_id, escalated_at, id) WHERE NOT is_deleted;

CREATE TRIGGER external_notice_escalations_set_updated_at BEFORE UPDATE ON external_notice_escalations FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER external_notice_escalations_append_only BEFORE UPDATE ON external_notice_escalations FOR EACH ROW EXECUTE FUNCTION reject_disclosure_content_change();

-- 2. Reviews of the recipient and of the package of one version. The digest is the version digest at the time of the
--    review, so a review always names the exact content it looked at.
CREATE TABLE external_notice_reviews (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    review_kind text NOT NULL CHECK (review_kind IN ('recipient', 'package')),
    case_id uuid NOT NULL REFERENCES cases (id),
    package_id uuid NOT NULL REFERENCES disclosure_packages (id),
    version_id uuid NOT NULL REFERENCES disclosure_versions (id),
    -- The package version number, copied from the version for display.
    version integer NOT NULL CHECK (version >= 1),
    digest text NOT NULL CHECK (digest ~ '^[0-9a-f]{64}$'),
    -- Only a recipient review names the recipient, by the same identifier a notice uses (kept outside SAFE).
    recipient_ref text CHECK (recipient_ref ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$'),
    reviewed_by uuid NOT NULL REFERENCES users (id),
    reviewed_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT external_notice_reviews_recipient_ref_only_for_recipient CHECK (
        (review_kind = 'recipient') = (recipient_ref IS NOT NULL)
    ),
    CONSTRAINT external_notice_reviews_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE INDEX external_notice_reviews_version_idx ON external_notice_reviews (version_id, reviewed_at, id) WHERE NOT is_deleted;

CREATE TRIGGER external_notice_reviews_set_updated_at BEFORE UPDATE ON external_notice_reviews FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER external_notice_reviews_append_only BEFORE UPDATE ON external_notice_reviews FOR EACH ROW EXECUTE FUNCTION reject_disclosure_content_change();
