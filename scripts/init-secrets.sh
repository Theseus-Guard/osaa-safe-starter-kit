#!/bin/sh
# Generates local secret files once. Never overwrites an existing file.
set -eu

root="$(cd "$(dirname "$0")/.." && pwd)"
dir="$root/secrets"

mkdir -p "$dir"
chmod 700 "$dir"

generate() {
  name="$1"
  if [ ! -f "$dir/$name.txt" ]; then
    umask 077
    openssl rand -hex 32 > "$dir/$name.txt"
    echo "generated secrets/$name.txt"
  else
    echo "secrets/$name.txt already exists; left unchanged"
  fi
}

generate db_password
generate db_migrator_password
generate db_app_password
generate db_worker_password
generate jwt_signing_key
# The evidence key must decode to exactly 32 bytes, so it is base64 of 32 random bytes.
if [ ! -f "$dir/evidence_kek.txt" ]; then
  umask 077
  openssl rand -base64 32 > "$dir/evidence_kek.txt"
  echo "generated secrets/evidence_kek.txt"
else
  echo "secrets/evidence_kek.txt already exists; left unchanged"
fi
