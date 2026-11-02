-- Index for the overview dashboard's own-action series (F2, plans/f1/overview-dashboard.md, "my contributions").
-- Each series reads the caller's audit events of a few actions over a 7, 30 or 90 day range. The date index of migration
-- 0031 scans every audit event of the range, and the target index of migration 0014 does not filter by actor, so this
-- index serves the actor, the action and the date range directly. The partial predicate matches the NOT is_deleted reads.
-- Forward only: this migration adds an index and nothing else.

CREATE INDEX audit_events_actor_action_created_idx ON audit_events (actor_id, action, created_at) WHERE NOT is_deleted;
