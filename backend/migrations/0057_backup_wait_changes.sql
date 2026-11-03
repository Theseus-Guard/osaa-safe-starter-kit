-- The history of changes to the backup waiting time (F3, owner decisions 2026-11-03; product manager recommendation applied).
--
-- The waiting time (external_notice_settings, key backup_wait_hours, migration 0054) was set in the database only. From
-- now on the institution owner changes it through the API (holders of disclosures:set_backup_wait, migration 0055), with
-- a reason. Each change writes one row here and one audit row, in the same transaction as the UPDATE of the setting.
--
-- The rows are insert-only: the trigger reject_disclosure_content_change (migration 0040) rejects any content change.
-- The reason is free text, kept in this row and never written to the audit log (the audit log keeps its length).
--
-- The default 4 hours of migration 0054 is unchanged. The owner must still confirm that number; it is not in the plans.

CREATE TABLE external_notice_setting_changes (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    setting_key text NOT NULL REFERENCES external_notice_settings (key),
    old_value integer NOT NULL CHECK (old_value BETWEEN 1 AND 168),
    new_value integer NOT NULL CHECK (new_value BETWEEN 1 AND 168),
    CONSTRAINT external_notice_setting_changes_differs CHECK (old_value <> new_value),
    -- The reason of the change: required, and bounded.
    reason text NOT NULL CHECK (char_length(reason) BETWEEN 1 AND 500),
    -- The institution owner who made the change.
    changed_by uuid NOT NULL REFERENCES users (id),
    changed_at timestamptz NOT NULL DEFAULT now(),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT external_notice_setting_changes_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE INDEX external_notice_setting_changes_key_idx ON external_notice_setting_changes (setting_key, changed_at, id) WHERE NOT is_deleted;

CREATE TRIGGER external_notice_setting_changes_set_updated_at BEFORE UPDATE ON external_notice_setting_changes FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER external_notice_setting_changes_append_only BEFORE UPDATE ON external_notice_setting_changes FOR EACH ROW EXECUTE FUNCTION reject_disclosure_content_change();
