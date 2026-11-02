-- The disclosure approver reads the case it approves (F2, owner decision on the approver read).
--
-- A disclosure version is shown with its case, so the approver needs a case read permission. cases:read_all is the one
-- that reaches a case without an assignment. The approver gets no other case permission: it is not assignable, so the
-- per-case rule cases:read_assigned could never reach it. The grant is wider than the case detail alone. It also
-- opens the preliminary report, the full free text and the full case history, which the owner accepted.
--
-- The deadline routes accept cases:read_all, so the approver would otherwise change deadline tasks. The application
-- refuses that for this role alone (api.py, the deadline routes), and the appeal route refuses it too.
--
-- Only this one row is added. The approver keeps disclosures:approve and disclosures:read from migrations 0007 and
-- 0040, and no other role changes. Forward only: a later migration may revoke the row, and this one never edits applied
-- migrations.

INSERT INTO role_permissions (role, permission) VALUES
    ('disclosure_approver', 'cases:read_all')
ON CONFLICT (role, permission) DO NOTHING;
