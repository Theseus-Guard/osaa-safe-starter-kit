-- The worker's stale-upload reconciliation (safe_kit.evidence.fail_stale_pending) reads evidence_items, sets its status,
-- and appends a custody event. Migration 0012 gave the worker only worker_heartbeat and outbox_jobs, so the
-- reconciliation failed with InsufficientPrivilege. These grants are column-limited where possible: the worker
-- may change only the status of an evidence row, and it can add custody events but not change them.
-- Each block runs only when its role exists, so the migrations also run against a plain database in tests.

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'safe_worker') THEN
    GRANT SELECT ON evidence_items TO safe_worker;
    GRANT UPDATE (status) ON evidence_items TO safe_worker;
    GRANT SELECT, INSERT ON custody_events TO safe_worker;
  END IF;
END
$$;
