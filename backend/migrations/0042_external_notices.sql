-- External notices of a disclosure version (F2, package P8; plans/f0/api-draft.md sections 2.5 and 3.9, plans/03 test T12,
-- plans/04 I0 manual notice).
--
-- A notice records that a person sent one approved disclosure version to one recipient, through one channel, at one
-- time, and what came back. Sending is manual: the application never sends, and it never resends. A notice starts as
-- 'sent'. Only a receipt from the recipient moves it to 'receipt_received'. Provider acceptance is not a receipt, and a
-- timeout or no answer is recorded as 'receipt_unknown', never as delivered.
--
-- Every change is a new revision row of the same notice (notice_id). A row carries a full copy of the notice's facts,
-- plus the state and the reason of the change that produced it. Rows are insert-only: the trigger
-- reject_disclosure_content_change (migration 0040) rejects any change except the soft-delete columns. The application
-- never deletes rows.
--
-- A notice may be created only against an approved version that is still the latest one of its package. The trigger
-- below keeps that rule for the first revision even if a write bypasses the application check.
--
-- The recipient is a reference (an identifier kept outside SAFE), never a name, an address or free text. The
-- channel is a closed list; 'other' needs a description.

-- 1. The notice rows.
CREATE TABLE external_notices (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    notice_id uuid NOT NULL,
    revision integer NOT NULL CHECK (revision >= 1),
    case_id uuid NOT NULL REFERENCES cases (id),
    package_id uuid NOT NULL REFERENCES disclosure_packages (id),
    version_id uuid NOT NULL REFERENCES disclosure_versions (id),
    -- The package version number, copied from the version for display.
    version integer NOT NULL CHECK (version >= 1),
    channel text NOT NULL CHECK (channel IN ('email', 'postal', 'webhook', 'secure_manual', 'other')),
    channel_other text CHECK (char_length(channel_other) BETWEEN 1 AND 200),
    recipient_ref text NOT NULL CHECK (recipient_ref ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$'),
    -- When the notice was sent (the person's statement), and when this record was first written.
    sent_at timestamptz NOT NULL,
    recorded_at timestamptz NOT NULL,
    recorded_by uuid NOT NULL REFERENCES users (id),
    -- The manual path: recorded by a person with disclosures:prepare, because the approver was unavailable.
    manual_fallback boolean NOT NULL DEFAULT false,
    fallback_reason text CHECK (char_length(fallback_reason) BETWEEN 1 AND 500),
    -- A second notice for the same version and recipient needs a reason. Copied to every revision of that notice.
    repeat_reason text CHECK (char_length(repeat_reason) BETWEEN 1 AND 500),
    state text NOT NULL CHECK (state IN (
        'sent', 'receipt_received', 'receipt_unknown', 'follow_up_required', 'follow_up_done', 'cancelled'
    )),
    -- The change that produced this revision.
    state_reason_code text CHECK (state_reason_code IN ('provider_accepted_no_receipt', 'timeout', 'no_answer')),
    state_reason text CHECK (char_length(state_reason) BETWEEN 1 AND 500),
    received_at timestamptz,
    changed_by uuid NOT NULL REFERENCES users (id),
    changed_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT external_notices_channel_other CHECK ((channel = 'other') = (channel_other IS NOT NULL)),
    CONSTRAINT external_notices_fallback_reason CHECK (manual_fallback = (fallback_reason IS NOT NULL)),
    CONSTRAINT external_notices_first_revision_state CHECK (revision > 1 OR state = 'sent'),
    CONSTRAINT external_notices_unknown_has_reason CHECK (state <> 'receipt_unknown' OR state_reason_code IS NOT NULL),
    CONSTRAINT external_notices_received_has_time CHECK (
        (state = 'receipt_received') = (received_at IS NOT NULL)
    ),
    CONSTRAINT external_notices_reason_given CHECK (
        state NOT IN ('follow_up_required', 'follow_up_done', 'cancelled') OR state_reason IS NOT NULL
    ),
    CONSTRAINT external_notices_reason_code_only_when_unknown CHECK (
        state_reason_code IS NULL OR state = 'receipt_unknown'
    ),
    CONSTRAINT external_notices_revision_unique UNIQUE (notice_id, revision),
    CONSTRAINT external_notices_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE INDEX external_notices_version_idx ON external_notices (version_id, recorded_at, notice_id) WHERE NOT is_deleted;
CREATE INDEX external_notices_case_idx ON external_notices (case_id, recorded_at, id) WHERE NOT is_deleted;

CREATE TRIGGER external_notices_set_updated_at BEFORE UPDATE ON external_notices FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER external_notices_append_only BEFORE UPDATE ON external_notices FOR EACH ROW EXECUTE FUNCTION reject_disclosure_content_change();

-- 2. A notice starts only against an approved version that is the latest of its package. The approval row and the
--    version row must belong to the same case and package; the application checks the same rule before it writes.
CREATE FUNCTION check_external_notice_start() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    v RECORD;
BEGIN
    IF NEW.revision <> 1 THEN
        RETURN NEW;
    END IF;
    SELECT dv.package_id, dv.case_id, dv.version
    INTO v
    FROM disclosure_versions dv
    WHERE dv.id = NEW.version_id AND NOT dv.is_deleted;
    IF NOT FOUND OR v.package_id <> NEW.package_id OR v.case_id <> NEW.case_id OR v.version <> NEW.version THEN
        RAISE EXCEPTION 'external notice refers to a version it does not belong to';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM disclosure_approvals da
        WHERE da.version_id = NEW.version_id AND NOT da.is_deleted
    ) THEN
        RAISE EXCEPTION 'external notice needs an approved disclosure version';
    END IF;
    IF EXISTS (
        SELECT 1 FROM disclosure_versions newer
        WHERE newer.package_id = NEW.package_id AND newer.version > NEW.version AND NOT newer.is_deleted
    ) THEN
        RAISE EXCEPTION 'external notice needs the latest version of its disclosure package';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER external_notices_start_check BEFORE INSERT ON external_notices
    FOR EACH ROW EXECUTE FUNCTION check_external_notice_start();
