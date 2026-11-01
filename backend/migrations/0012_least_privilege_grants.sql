-- Least-privilege grants that depend on the table list. Each block runs only when its role exists,
-- so the migrations also run against a plain database in tests.

DO $$
BEGIN
  -- The migration ledger is written only by the migration runner (its owner). The API reads it for readiness.
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'safe_api') THEN
    REVOKE INSERT, UPDATE, DELETE ON schema_migrations FROM safe_api;
  END IF;
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'safe_worker') THEN
    REVOKE ALL ON schema_migrations FROM safe_worker;
  END IF;

  -- The worker needs exactly two tables: its heartbeat and the outbox it processes.
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'safe_worker') THEN
    REVOKE ALL ON ALL TABLES IN SCHEMA public FROM safe_worker;
    REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM safe_worker;
    GRANT SELECT, INSERT, UPDATE ON worker_heartbeat, outbox_jobs TO safe_worker;
  END IF;
END
$$;
