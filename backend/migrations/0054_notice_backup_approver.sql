-- The backup approver of an external notice (F3, work package A6; owner decisions 2026-11-03; plans/02 disclosure flow).
--
-- When the approver cannot send an urgent notice, the preparer of the version opens an escalation (its table is created by the earlier T12-A migration) and names one
-- backup. This migration adds the rules that make that backup real:
--
-- 1. external_notice_settings: the waiting time before a backup may act (key backup_wait_hours, default 4 hours). The
--    escalation copies the value it had when it was opened, so a later change does not move an open escalation.
-- 2. external_notice_escalations.backup_may_act_at: the first moment the named backup may decide (escalated_at plus the
--    waiting time). The backup cannot act earlier.
-- 3. external_notice_backups: the backup pool of one case. The owner assigns it; the application keeps it at two people
--    or more. A removal is a soft delete (is_deleted), so the row is kept.
-- 4. external_notice_backup_decisions: the backup's decision on an escalation: approve, reject (with a reason) or
--    emergency_manual_notice. One decision per escalation. Insert-only.
--
-- The application checks the rules before it writes (safe_kit.external_notice_backup and
-- safe_kit.external_notice_records): the owner assigns the pool, the pool holds two people or more, the backup is in the
-- pool, the backup holds disclosures:approve, the preparer opens the escalation, and the wait is over. The database keeps
-- the shape of each row, the wait order, and the insert-only rule.
--
-- The permission disclosures:assign_backup is added here and granted to no role. The owner decides which role holds it.

-- 1. The permission to assign a backup pool. A holder of disclosures:approve may never use it (the application refuses it).
INSERT INTO permissions (name, description) VALUES
    ('disclosures:assign_backup', 'Assign the backup approvers of a case (owner only; never a holder of disclosures:approve)')
ON CONFLICT (name) DO NOTHING;

-- 2. Settings of the external notice records. A setting is a row keyed by its name; a soft delete removes its effect.
CREATE TABLE external_notice_settings (
    key text PRIMARY KEY CHECK (key IN ('backup_wait_hours')),
    -- Hours the backup waits after an escalation is opened. Between one hour and one week.
    value integer NOT NULL CHECK (value BETWEEN 1 AND 168),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT external_notice_settings_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE TRIGGER external_notice_settings_set_updated_at BEFORE UPDATE ON external_notice_settings FOR EACH ROW EXECUTE FUNCTION set_updated_at();

INSERT INTO external_notice_settings (key, value) VALUES ('backup_wait_hours', 4);

-- 3. The waiting time of each escalation. The default only covers rows that exist before this migration; the application
--    always sets the value from external_notice_settings when it opens an escalation.
ALTER TABLE external_notice_escalations
    ADD COLUMN backup_may_act_at timestamptz NOT NULL DEFAULT (now() + interval '4 hours'),
    ADD CONSTRAINT external_notice_escalations_wait_after_open CHECK (backup_may_act_at >= escalated_at);

-- 4. The backup pool of one case. A backup is removed by a soft delete; a removed person can be assigned again as a new row.
CREATE TABLE external_notice_backups (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES cases (id),
    backup_user_id uuid NOT NULL REFERENCES users (id),
    -- The owner who assigned the backup (a holder of disclosures:assign_backup).
    assigned_by uuid NOT NULL REFERENCES users (id),
    assigned_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT external_notice_backups_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE UNIQUE INDEX external_notice_backups_active_idx ON external_notice_backups (case_id, backup_user_id) WHERE NOT is_deleted;
CREATE INDEX external_notice_backups_case_idx ON external_notice_backups (case_id, assigned_at, id) WHERE NOT is_deleted;

CREATE TRIGGER external_notice_backups_set_updated_at BEFORE UPDATE ON external_notice_backups FOR EACH ROW EXECUTE FUNCTION set_updated_at();
-- Only the soft-delete columns may change (the function is shared with the disclosure records, migration 0040).
CREATE TRIGGER external_notice_backups_append_only BEFORE UPDATE ON external_notice_backups FOR EACH ROW EXECUTE FUNCTION reject_disclosure_content_change();

-- 5. The backup's decision on one escalation. The backup named on the escalation makes it, and only once.
CREATE TABLE external_notice_backup_decisions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    escalation_id uuid NOT NULL REFERENCES external_notice_escalations (id),
    case_id uuid NOT NULL REFERENCES cases (id),
    version_id uuid NOT NULL REFERENCES disclosure_versions (id),
    decision text NOT NULL CHECK (decision IN ('approve', 'reject', 'emergency_manual_notice')),
    -- A reject needs a reason; the other decisions may carry one.
    reason text CHECK (reason IS NULL OR char_length(reason) BETWEEN 1 AND 500),
    CONSTRAINT external_notice_backup_decisions_reject_has_reason CHECK (decision <> 'reject' OR reason IS NOT NULL),
    decided_by uuid NOT NULL REFERENCES users (id),
    decided_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT external_notice_backup_decisions_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE UNIQUE INDEX external_notice_backup_decisions_escalation_idx ON external_notice_backup_decisions (escalation_id) WHERE NOT is_deleted;

CREATE TRIGGER external_notice_backup_decisions_set_updated_at BEFORE UPDATE ON external_notice_backup_decisions FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER external_notice_backup_decisions_append_only BEFORE UPDATE ON external_notice_backup_decisions FOR EACH ROW EXECUTE FUNCTION reject_disclosure_content_change();
