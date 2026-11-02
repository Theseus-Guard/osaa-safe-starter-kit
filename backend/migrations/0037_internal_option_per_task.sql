-- Internal incident option, per task (owner decision, F2).
-- The case-wide switch is gone. Each external notice task (affected_customers, preliminary_safe_report,
-- broader_advisory, preliminary_public_report) is marked not applicable on its own, with a reason, through the deadline
-- update. Due dates never change, and no task is deleted.
--
-- Existing data keeps its meaning. A case whose current classification (its highest version) has
-- internal_incident_option = true gets the same not-applicable exception on each of its external notice tasks, with a
-- reason that records the migration. A task that is already not applicable keeps its status and only takes the
-- migrated reason. The classification versions are insert-only history and are not changed; the column stays.
--
-- Each change writes one deadline audit row with no actor (the migration is not a person) and source migration_0037.
-- Free text is never written to the audit log, so the reason appears there only as its length.
-- Forward only. Re-running it changes nothing: once a task carries the migrated reason, it is not selected again.

WITH current_option AS (
    SELECT DISTINCT ON (case_id) case_id, internal_incident_option
    FROM case_classifications
    WHERE NOT is_deleted
    ORDER BY case_id, version DESC
),
targets AS (
    SELECT t.id AS task_id, t.case_id, t.kind, t.status AS old_status, t.not_applicable_reason AS old_reason
    FROM deadline_tasks t
    JOIN current_option c ON c.case_id = t.case_id AND c.internal_incident_option
    WHERE NOT t.is_deleted
      AND t.kind IN ('affected_customers', 'preliminary_safe_report', 'broader_advisory', 'preliminary_public_report')
      AND (t.status <> 'not_applicable'
           OR t.not_applicable_reason IS DISTINCT FROM 'migrated: the case-wide internal incident option was set on this case')
),
updated AS (
    UPDATE deadline_tasks t
    SET status = 'not_applicable',
        not_applicable_reason = 'migrated: the case-wide internal incident option was set on this case'
    FROM targets x
    WHERE t.id = x.task_id
    RETURNING x.task_id, x.case_id, x.kind, x.old_status, x.old_reason
)
INSERT INTO audit_events (actor_id, action, target_type, target_id, details)
SELECT
    NULL,
    'case.deadline_updated',
    'case',
    u.case_id::text,
    jsonb_build_object('task_id', u.task_id::text, 'kind', u.kind, 'source', 'migration_0037')
    || CASE WHEN u.old_status <> 'not_applicable'
            THEN jsonb_build_object('status', jsonb_build_object('old', u.old_status, 'new', 'not_applicable'))
            ELSE '{}'::jsonb END
    || jsonb_build_object(
        'not_applicable_reason',
        jsonb_build_object(
            'old', CASE WHEN u.old_reason IS NULL THEN NULL ELSE jsonb_build_object('length', char_length(u.old_reason)) END,
            'new', jsonb_build_object('length', char_length('migrated: the case-wide internal incident option was set on this case'))
        )
    )
FROM updated u;
