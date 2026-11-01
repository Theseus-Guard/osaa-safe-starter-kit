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
generate jwt_signing_key
