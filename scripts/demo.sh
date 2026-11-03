#!/bin/sh
# Runs the three-step demo on this machine. Loopback only; synthetic data only.
#   scripts/demo.sh start [--timeout N]   build and start the demo stack, seed it once, print the loopback URL
#   scripts/demo.sh status                service states, readiness, and which demo credential files exist
#   scripts/demo.sh cli-show CASE_ID      read one case with the CLI, --json, as a demo role (default demo-investigator)
#
# cli-show runs the CLI on this machine (uv run in backend/), through the published loopback gateway. It does not run
# the cli container: the API accepts demo sign-in only from a loopback address or SAFE_DEMO_HOST_ADDRESS, and the cli
# container has neither (it sits on the internal backend network). The trust rule is not widened for it. The gateway's
# development CA (secrets/tls/ca.crt, or SAFE_DEV_CA_FILE) is trusted for that one CLI process through SSL_CERT_FILE.
#
# SAFE_DEMO_DIR is required and has no default. Name a directory you choose, as an absolute path outside this
# repository. The script creates SAFE_DEMO_DIR and SAFE_DEMO_DIR/credentials (mode 0700) and sets
# SAFE_DEMO_CREDENTIAL_DIR to the second one. The seed writes one credential file per demo role there (mode 0600).
# No credential is ever printed, logged, or put in an argument or environment variable.
#
# --timeout N bounds the readiness wait of start (whole seconds, default 300).
# The sample case for step three is 5afe0d00-0000-4000-8000-000000000201 (demo: tool call outside the approved scope).
# Exit codes: 0 done, 1 failed, 2 usage error.
set -eu

root="$(cd "$(dirname "$0")/.." && pwd)"
project="safe-demo"
url="https://127.0.0.1:8443"
# The demo role whose credential the CLI uses. It is assigned to the sample case.
cli_user="demo-investigator"
# One credential file per demo role (the roles of safe_kit.demo_auth.DEMO_ROLES).
demo_users="demo-admin demo-reporter demo-triager demo-investigator demo-reviewer demo-approver demo-auditor"

usage() {
  echo "usage: scripts/demo.sh start [--timeout N] | status | cli-show CASE_ID" >&2
}

fail() {
  echo "demo: $*" >&2
  exit 1
}

require_demo_dir() {
  if [ -z "${SAFE_DEMO_DIR:-}" ]; then
    echo "demo: SAFE_DEMO_DIR is required: name a directory for the demo files, for example SAFE_DEMO_DIR=\$HOME/safe-demo" >&2
    exit 2
  fi
  case "$SAFE_DEMO_DIR" in
    /*) ;;
    *) echo "demo: SAFE_DEMO_DIR must be an absolute path" >&2; exit 2 ;;
  esac
  case "$SAFE_DEMO_DIR" in
    "$root"|"$root"/*) echo "demo: SAFE_DEMO_DIR must be outside this repository" >&2; exit 2 ;;
  esac
  credential_dir="$SAFE_DEMO_DIR/credentials"
}

require_docker() {
  command -v docker >/dev/null 2>&1 || fail "docker is not installed; install Docker Engine with the Compose plugin first"
  docker info >/dev/null 2>&1 || fail "docker is not running; start the Docker daemon and try again"
  docker compose version >/dev/null 2>&1 || fail "the docker compose plugin is not available"
}

require_curl() {
  command -v curl >/dev/null 2>&1 || fail "curl is required for the readiness check"
}

# The demo project: its own name, so it never shares containers, networks or volumes with another stack.
compose() {
  SAFE_DEMO_CREDENTIAL_DIR="$credential_dir" docker compose -p "$project" \
    -f "$root/compose.yaml" -f "$root/compose.demo.yaml" "$@"
}

# Ready means the gateway answers and the API behind it reports ready. The probe is loopback only and sends no
# credential. -k accepts the development certificate that scripts/make-dev-cert.sh creates.
gateway_ready() {
  curl -ksf --max-time 5 -o /dev/null "$url/api/v1/health/ready"
}

# The probe alone cannot say which stack answered: another Compose project may publish the same loopback port (the
# owner's safe-starter-kit gateway holds 127.0.0.1:8443). So ready also needs this project's own gateway container
# to be running.
demo_ready() {
  [ -n "$(compose ps --status running -q gateway 2>/dev/null)" ] && gateway_ready
}

# The seed runs as the container user (uid 10001) and writes into the operator's directory. The directory and its
# files are handed to that user for the seed, and back to the operator afterwards, so the operator can read them.
# This one-off run uses the api image as root with only the CHOWN capability added (the service drops all capabilities).
hand_over() {
  compose run --rm -T --no-deps --user 0:0 --cap-add CHOWN --entrypoint chown api -R "$1" /run/safe-demo >/dev/null
}

cmd_start() {
  timeout=300
  while [ $# -gt 0 ]; do
    case "$1" in
      --timeout)
        if [ $# -lt 2 ]; then usage; exit 2; fi
        timeout="$2"
        shift 2
        ;;
      *)
        usage
        exit 2
        ;;
    esac
  done
  case "$timeout" in
    ''|*[!0-9]*) echo "demo: --timeout takes a whole number of seconds" >&2; exit 2 ;;
  esac
  if [ "$timeout" -lt 1 ]; then
    echo "demo: --timeout must be at least 1 second" >&2
    exit 2
  fi

  require_demo_dir
  require_docker
  require_curl

  # The compose file reads its secrets from ./secrets and the gateway certificate from ./secrets/tls. Both scripts
  # create only what is missing and never overwrite an existing file.
  "$root/scripts/init-secrets.sh"
  "$root/scripts/make-dev-cert.sh"

  umask 077
  mkdir -p "$SAFE_DEMO_DIR" "$credential_dir"
  chmod 700 "$credential_dir"

  echo "demo: building and starting the stack (project $project)"
  compose up -d --build

  deadline=$(( $(date +%s) + timeout ))
  until demo_ready; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
      fail "the demo did not report ready within $timeout seconds; run scripts/demo.sh status"
    fi
    sleep 2
  done
  echo "demo: gateway and API report ready"

  seed_status=0
  hand_over 10001:10001 || fail "the credential directory could not be handed to the container user"
  if ! compose exec -T api python -m safe_kit.demo_seed seed; then
    seed_status=1
  fi
  hand_over "$(id -u):$(id -g)" || seed_status=1
  if [ "$seed_status" -ne 0 ]; then
    fail "the demo seed failed (see the message above); the credential directory may need attention, then run start again"
  fi

  echo "demo: ready. Open $url in a browser on this machine, accept the development certificate,"
  echo "demo: and sign in as a demo role. The credentials are in the credentials directory under SAFE_DEMO_DIR;"
  echo "demo: they are not printed here."
}

cmd_status() {
  if [ $# -gt 0 ]; then usage; exit 2; fi
  require_demo_dir
  require_docker
  compose ps
  if command -v curl >/dev/null 2>&1 && demo_ready; then
    echo "demo: ready at $url"
  else
    echo "demo: not ready at $url"
  fi
  for user in $demo_users; do
    if [ -f "$credential_dir/$user.credential" ]; then
      echo "demo: $user credential file present"
    else
      echo "demo: $user credential file missing"
    fi
  done
}

cmd_cli_show() {
  require_demo_dir
  if [ $# -ne 1 ]; then usage; exit 2; fi
  case_id="$1"
  if ! printf '%s\n' "$case_id" | grep -Eq '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'; then
    echo "demo: CASE_ID must be a case ID (UUID)" >&2
    exit 2
  fi
  demo_user="${SAFE_DEMO_USERNAME:-$cli_user}"
  case " $demo_users " in
    *" $demo_user "*) ;;
    *) echo "demo: SAFE_DEMO_USERNAME must be one of the demo roles: $demo_users" >&2; exit 2 ;;
  esac
  # The credential file is passed by path. The CLI reads its first line; the value never reaches this script's
  # arguments, environment, or output.
  credential_file="$credential_dir/$demo_user.credential"
  if [ ! -f "$credential_file" ]; then
    fail "no credential file for $demo_user; run scripts/demo.sh start first"
  fi
  command -v uv >/dev/null 2>&1 || fail "uv is required to run the CLI on this machine; install uv, then run: uv sync in backend/"
  # The gateway's certificate is issued by the development CA. Only this one CLI process trusts that CA.
  tls_ca="${SAFE_DEV_CA_FILE:-$root/secrets/tls/ca.crt}"
  if [ ! -f "$tls_ca" ]; then
    fail "the development CA is missing at $tls_ca; run scripts/demo.sh start first"
  fi
  # PYTHONPATH is the same source directory the API image uses (backend/Dockerfile). The project is not installed.
  SAFE_API_URL="$url" SSL_CERT_FILE="$tls_ca" PYTHONPATH="$root/backend/src" \
    uv run --quiet --project "$root/backend" python -m safe_kit.cli --no-banner case show "$case_id" --json \
    --demo-username "$demo_user" --demo-credential-file "$credential_file"
}

command_name="${1:-}"
if [ $# -gt 0 ]; then
  shift
fi
case "$command_name" in
  start) cmd_start "$@" ;;
  status) cmd_status "$@" ;;
  cli-show) cmd_cli_show "$@" ;;
  *) usage; exit 2 ;;
esac
