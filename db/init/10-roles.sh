#!/bin/sh
# Creates or updates the SAFE database roles. Idempotent: safe to run on every start.
#
# Runs in two places, with the same file:
#   - docker-entrypoint-initdb.d, on a fresh volume only (db service, local socket);
#   - the one-shot db-roles service, on an existing volume (PGHOST=db), see compose.yaml.
#
# Role model (threat TM-07):
#   safe_migrator  owns the database, the public schema and every object in it; runs migrations.
#   safe_api       SELECT, INSERT, UPDATE on tables and sequences. No DELETE, no DDL, no ownership.
#   safe_worker    same DML grants as safe_api. No DDL, no ownership.
# POSTGRES_USER is the bootstrap superuser. Only this script uses it, through the local socket.
# Its password is replaced by a random value on every run and never stored, so it cannot be used over the network.
# Application role passwords come from secret files. ALTER ROLE re-applies them on every run.
set -eu

: "${POSTGRES_USER:?POSTGRES_USER must be set}"
POSTGRES_DB="${POSTGRES_DB:-$POSTGRES_USER}"

for name in db_migrator_password db_app_password db_worker_password; do
  if [ ! -s "/run/secrets/$name" ]; then
    echo "missing or empty secret: /run/secrets/$name" >&2
    exit 1
  fi
done

# Connect through the local socket (trusted inside the container). No superuser password is needed or kept.
PGHOST="${PGHOST:-/var/run/postgresql}"
export PGHOST

# Passwords are read by psql from the secret files (backtick \set), so they never appear in argv or in this script.
psql --no-psqlrc -X -q -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  -v bootstrap_user="$POSTGRES_USER" <<'SQL'
\set migrator_pw `cat /run/secrets/db_migrator_password`
\set app_pw `cat /run/secrets/db_app_password`
\set worker_pw `cat /run/secrets/db_worker_password`
\set bootstrap_pw `head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n'`

-- 0. Replace the bootstrap superuser password with a random one that is never stored.
SELECT format('ALTER ROLE %I PASSWORD %L', :'bootstrap_user', :'bootstrap_pw') \gexec

-- 1. Roles: create if missing, then re-apply attributes and password.
SELECT format('CREATE ROLE %I', 'safe_migrator') WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'safe_migrator') \gexec
SELECT format('CREATE ROLE %I', 'safe_api') WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'safe_api') \gexec
SELECT format('CREATE ROLE %I', 'safe_worker') WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'safe_worker') \gexec
SELECT format('ALTER ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L', 'safe_migrator', :'migrator_pw') \gexec
SELECT format('ALTER ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L', 'safe_api', :'app_pw') \gexec
SELECT format('ALTER ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD %L', 'safe_worker', :'worker_pw') \gexec

-- 2. Database and public schema belong to the migration owner. Only the owner can create objects in them.
SELECT format('ALTER DATABASE %I OWNER TO safe_migrator', current_database()) \gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database()) \gexec
SELECT format('GRANT CONNECT ON DATABASE %I TO safe_migrator, safe_api, safe_worker', current_database()) \gexec
ALTER SCHEMA public OWNER TO safe_migrator;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO safe_migrator, safe_api, safe_worker;

-- 3. Existing objects (upgrade path): hand ownership to the migration owner. Idempotent.
DO $$
DECLARE
  obj record;
BEGIN
  FOR obj IN
    SELECT c.relname, c.relkind
    FROM pg_class c
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE n.nspname = 'public'
      AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
      AND pg_get_userbyid(c.relowner) <> 'safe_migrator'
  LOOP
    EXECUTE format(
      'ALTER %s public.%I OWNER TO safe_migrator',
      CASE obj.relkind
        WHEN 'S' THEN 'SEQUENCE'
        WHEN 'v' THEN 'VIEW'
        WHEN 'm' THEN 'MATERIALIZED VIEW'
        WHEN 'f' THEN 'FOREIGN TABLE'
        ELSE 'TABLE'
      END,
      obj.relname
    );
  END LOOP;

  FOR obj IN
    SELECT p.oid::regprocedure AS signature
    FROM pg_proc p
    JOIN pg_namespace n ON n.oid = p.pronamespace
    WHERE n.nspname = 'public'
      AND pg_get_userbyid(p.proowner) <> 'safe_migrator'
  LOOP
    EXECUTE format('ALTER FUNCTION %s OWNER TO safe_migrator', obj.signature);
  END LOOP;
END
$$;

-- 4. DML grants on existing objects for the API. The worker's grants are table-specific and live in migration 0012.
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO safe_api;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO safe_api;

-- The migration ledger: the API reads it for readiness; migration 0012 keeps it write-protected for the API.
DO $$
BEGIN
  IF to_regclass('public.schema_migrations') IS NOT NULL THEN
    REVOKE INSERT, UPDATE, DELETE ON TABLE public.schema_migrations FROM safe_api, safe_worker;
  END IF;
END
$$;

-- 5. Default privileges: new tables and sequences give the API DML. The worker gets nothing by default.
ALTER DEFAULT PRIVILEGES FOR ROLE safe_migrator IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE ON TABLES TO safe_api;
ALTER DEFAULT PRIVILEGES FOR ROLE safe_migrator IN SCHEMA public
  GRANT USAGE, SELECT ON SEQUENCES TO safe_api;
ALTER DEFAULT PRIVILEGES FOR ROLE safe_migrator IN SCHEMA public
  REVOKE ALL ON TABLES FROM safe_worker;
ALTER DEFAULT PRIVILEGES FOR ROLE safe_migrator IN SCHEMA public
  REVOKE ALL ON SEQUENCES FROM safe_worker;

-- 6. Guard: no application role may be a superuser or bypass row-level security.
DO $$
BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_roles
    WHERE rolname IN ('safe_migrator', 'safe_api', 'safe_worker')
      AND (rolsuper OR rolbypassrls)
  ) THEN
    RAISE EXCEPTION 'an application role is a superuser or bypasses RLS';
  END IF;
END
$$;
SQL

echo "SAFE database roles are in place (safe_migrator, safe_api, safe_worker)."
