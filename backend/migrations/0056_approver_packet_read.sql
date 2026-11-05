-- The disclosure approver reads the packet it approves, not every case (F3, owner decisions 2026-11-03; product manager
-- recommendation applied).
--
-- Migration 0044 gave the approver cases:read_all, so the approver could read every case, its full free text, its
-- history and its preliminary report. That is wider than the approval needs. This migration soft-deletes that one grant.
--
-- The approver keeps disclosures:approve and disclosures:read (migrations 0007 and 0040). The disclosure read path
-- (safe_kit.disclosure) gives the approver the version it may approve, which is the packet awaiting approval, and the
-- version it approved itself. The escalation read gives the missing facts of those versions. Nothing else of the case is
-- readable through the approver's role.
--
-- A soft delete (is_deleted, deleted_at), as every other removal in the system. The row stays for the history of the
-- role matrix; the role matrix reads only rows that are not deleted (safe_kit.rbac).

UPDATE role_permissions
SET is_deleted = true, deleted_at = now()
WHERE role = 'disclosure_approver' AND permission = 'cases:read_all' AND NOT is_deleted;
