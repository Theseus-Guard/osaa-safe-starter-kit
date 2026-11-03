-- F3 A5: the 30-day analysis obligation is a deadline task of its own (plans/01, the t0 + 30 days row: the preliminary
-- public report and the preliminary control-failure analysis). Its kind is added to the allowed kinds of deadline_tasks.
-- The check is widened, so every existing row stays valid.
--
-- Existing cases get the task once, with the due time the rule gives: t0 + 720 hours (30 elapsed days, the same instant
-- as timedelta(days=30) on UTC in deadlines.py, so the first resync does not move it). t0 is the earliest of the case
-- awareness_time and its operator awareness events (deadlines.deadline_start). The insert skips a case that already has
-- the kind (unique on case_id and kind), so running this again adds nothing. No row is deleted. Forward only.

ALTER TABLE deadline_tasks DROP CONSTRAINT deadline_tasks_kind_check;

ALTER TABLE deadline_tasks ADD CONSTRAINT deadline_tasks_kind_check CHECK (kind IN (
    'affected_customers',
    'preliminary_safe_report',
    'broader_advisory',
    'preliminary_public_report',
    'remediation_status',
    'weekly_update',
    'preliminary_control_failure_analysis'
));

INSERT INTO deadline_tasks (case_id, kind, due_at, status)
SELECT c.id,
       'preliminary_control_failure_analysis',
       t0.start_at + interval '720 hours',
       'open'
FROM cases c
CROSS JOIN LATERAL (
    -- LEAST ignores NULL, so a case without operator events starts at its awareness_time.
    SELECT LEAST(c.awareness_time, MIN(e.occurred_at)) AS start_at
    FROM awareness_events e
    WHERE e.case_id = c.id AND e.awareness_kind = 'operator' AND NOT e.is_deleted
) t0
WHERE NOT c.is_deleted
ON CONFLICT (case_id, kind) DO NOTHING;
