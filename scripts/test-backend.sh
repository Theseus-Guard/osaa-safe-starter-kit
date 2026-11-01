#!/bin/sh
# Runs the backend test suite with coverage.
#   scripts/test-backend.sh          unit and API tests only (no database needed)
#   scripts/test-backend.sh --db     also runs database tests against a throwaway PostgreSQL container
set -eu

root="$(cd "$(dirname "$0")/.." && pwd)"
cd "$root/backend"

if [ "${1:-}" != "--db" ]; then
  exec uv run pytest -q --cov=safe_kit --cov-report=term-missing
fi

port="${SAFE_TEST_DB_PORT:-55432}"
name="safe-test-pg-$$"
workdir="$(mktemp -d)"
pw_file="$workdir/password"
trap 'docker rm -f "$name" >/dev/null 2>&1 || true; rm -rf "$workdir"' EXIT INT TERM

umask 077
openssl rand -hex 24 > "$pw_file"

docker run -d --name "$name" -e POSTGRES_PASSWORD="$(cat "$pw_file")" -e POSTGRES_DB=safe_test \
  -p "127.0.0.1:$port:5432" postgres:18.6-trixie >/dev/null

i=0
until docker exec "$name" pg_isready -U postgres -d safe_test >/dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -gt 60 ]; then
    echo "test database did not become ready" >&2
    exit 1
  fi
  sleep 1
done

SAFE_TEST_DATABASE=1 \
SAFE_DB_HOST=127.0.0.1 \
SAFE_DB_PORT="$port" \
SAFE_DB_NAME=safe_test \
SAFE_DB_USER=postgres \
SAFE_DB_PASSWORD_FILE="$pw_file" \
  uv run pytest -q --cov=safe_kit --cov-report=term-missing
