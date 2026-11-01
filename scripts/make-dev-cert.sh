#!/bin/sh
# Creates a local development CA and a TLS server certificate for the gateway.
# Output directory: $SAFE_TLS_DIR, default secrets/tls. Existing files are never overwritten.
# Names: localhost, 127.0.0.1, safe-gateway.localhost. Development use only; see gateway/README-TLS.md.
set -eu

root="$(cd "$(dirname "$0")/.." && pwd)"
dir="${SAFE_TLS_DIR:-$root/secrets/tls}"

if ! command -v openssl >/dev/null 2>&1; then
  echo "openssl is required but was not found in PATH." >&2
  exit 1
fi

umask 077
mkdir -p "$dir"
chmod 700 "$dir"
dir="$(cd "$dir" && pwd)"

ca_key="$dir/ca.key"
ca_crt="$dir/ca.crt"
srv_key="$dir/server.key"
srv_crt="$dir/server.crt"

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT INT TERM

# Extension and naming settings live in a file, so the script works with LibreSSL and OpenSSL.
cat > "$work/openssl.cnf" <<'EOF'
[req]
distinguished_name = req_dn
prompt = no

[req_dn]
CN = placeholder

[ca_ext]
basicConstraints = critical,CA:TRUE,pathlen:0
keyUsage = critical,keyCertSign,cRLSign
subjectKeyIdentifier = hash

[srv_ext]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid,issuer
subjectAltName = @alt_names

[alt_names]
DNS.1 = localhost
DNS.2 = safe-gateway.localhost
IP.1 = 127.0.0.1
EOF

ca_existed=0
if [ -e "$ca_key" ] || [ -e "$ca_crt" ]; then
  if [ ! -f "$ca_key" ] || [ ! -f "$ca_crt" ]; then
    echo "Incomplete CA in $dir: ca.key or ca.crt is missing. Nothing was changed; fix it manually." >&2
    exit 1
  fi
  ca_existed=1
  echo "CA already exists; left unchanged: $ca_crt"
else
  openssl ecparam -name prime256v1 -genkey -noout -out "$ca_key"
  chmod 600 "$ca_key"
  openssl req -x509 -new -key "$ca_key" -sha256 -days 3650 \
    -subj "/CN=SAFE Starter Kit Local Dev CA" \
    -config "$work/openssl.cnf" -extensions ca_ext \
    -out "$ca_crt"
  chmod 644 "$ca_crt"
  echo "Created CA: $ca_crt"
fi

if [ -e "$srv_key" ] || [ -e "$srv_crt" ]; then
  if [ ! -f "$srv_key" ] || [ ! -f "$srv_crt" ]; then
    echo "Incomplete server certificate in $dir: server.key or server.crt is missing. Nothing was changed for it; fix it manually." >&2
    exit 1
  fi
  echo "Server certificate already exists; left unchanged: $srv_crt"
  if [ "$ca_existed" -eq 1 ]; then
    echo "Nothing to do. To issue a new certificate, move the existing server.* files out of $dir first."
  fi
else
  openssl ecparam -name prime256v1 -genkey -noout -out "$srv_key"
  chmod 600 "$srv_key"
  openssl req -new -key "$srv_key" \
    -subj "/CN=localhost" \
    -config "$work/openssl.cnf" \
    -out "$work/server.csr"
  openssl x509 -req -in "$work/server.csr" -sha256 -days 825 \
    -CA "$ca_crt" -CAkey "$ca_key" \
    -set_serial "0x$(openssl rand -hex 15)" \
    -extfile "$work/openssl.cnf" -extensions srv_ext \
    -out "$srv_crt"
  chmod 644 "$srv_crt"
  echo "Created server certificate (valid 825 days): $srv_crt"
fi

openssl verify -CAfile "$ca_crt" "$srv_crt"

echo ""
echo "CA certificate path: $ca_crt"
echo "Trust the CA once on your machine (one command per OS):"
echo "  macOS:  sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain \"$ca_crt\""
echo "  Ubuntu: sudo cp \"$ca_crt\" /usr/local/share/ca-certificates/safe-starter-kit-dev-ca.crt && sudo update-ca-certificates"
echo "Keep ca.key private: anyone holding it can sign certificates that this machine trusts."
