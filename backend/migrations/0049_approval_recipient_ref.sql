-- Recipient reference of a disclosure approval (F2, gap C: a notice is bound to the approved recipient; plans/03 T12).
--
-- An approval names the recipient as free text (disclosure_approvals.recipient, migration 0040). A notice names its
-- recipient by an identifier kept outside SAFE (external_notices.recipient_ref, migration 0042). The two forms differ,
-- so the approver also confirms that identifier when approving, in recipient_ref. A notice of the version is accepted
-- only when its recipient_ref equals this value (safe_kit.external_notices).
--
-- Existing approvals have no recipient_ref and keep NULL: the check below is NOT VALID, so it applies to new rows only.
-- A version approved before this migration cannot receive a notice (the notice is refused as recipient_mismatch). Its
-- package needs a new version and a new approval that names the identifier.
--
-- The column is written only at approval time. Adding a column changes no row, so the insert-only rule of
-- disclosure_approvals (migration 0040) still holds.

ALTER TABLE disclosure_approvals
    ADD COLUMN recipient_ref text;

ALTER TABLE disclosure_approvals
    ADD CONSTRAINT disclosure_approvals_recipient_ref_set
    CHECK (recipient_ref IS NOT NULL AND recipient_ref ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$') NOT VALID;
