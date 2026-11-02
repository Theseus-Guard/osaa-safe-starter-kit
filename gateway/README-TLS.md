# Gateway TLS

The gateway serves HTTPS only, on container port 8443. Compose publishes it on `127.0.0.1:8443`
(`SAFE_GATEWAY_BIND` changes the bind address). There is no plain-HTTP listener and no HTTP redirect.
The URL is **https://127.0.0.1:8443/**. In browsers that resolve `*.localhost`, `https://safe-gateway.localhost:8443/` also works.

Without the certificate files the gateway refuses to start. There is no HTTP fallback.

## 1. Create a development CA and certificate

```sh
sh scripts/make-dev-cert.sh
```

This writes `secrets/tls/ca.crt`, `ca.key`, `server.crt` and `server.key`. The server certificate covers
`localhost`, `127.0.0.1` and `safe-gateway.localhost`, is valid for 825 days, and is signed by a CA valid for 10 years.
Existing files are never overwritten. To issue a new server certificate, move the `server.*` files out first.
Keep `ca.key` private. Anyone who holds it can sign certificates that your machine trusts.

Then start the gateway:

```sh
docker compose up -d --build --no-deps --wait gateway
```

## 2. Trust the CA (once per machine)

macOS:

```sh
sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain secrets/tls/ca.crt
```

Ubuntu:

```sh
sudo cp secrets/tls/ca.crt /usr/local/share/ca-certificates/safe-starter-kit-dev-ca.crt && sudo update-ca-certificates
```

Firefox and Chromium on Linux may use their own certificate stores. Import `ca.crt` there if browsers still warn.
Check the setup from the command line:

```sh
curl --cacert secrets/tls/ca.crt https://127.0.0.1:8443/api/v1/health/ready
```

## 3. Replace with an organization certificate

The compose file reads two files, both PEM:

- `secrets/tls/server.crt`: the leaf certificate. Append the intermediate chain after it, leaf first.
- `secrets/tls/server.key`: the private key, unencrypted (nginx does not prompt for a passphrase). Mode 600.

Requirements: the certificate's subjectAltName must include the host name users type. The key must match the certificate.
Use RSA 2048 or larger, or an ECDSA P-256 or larger key. Check the match:

```sh
openssl x509 -in secrets/tls/server.crt -noout -pubkey | openssl sha256
openssl pkey -in secrets/tls/server.key -pubout | openssl sha256
```

The two hashes must be equal. Then recreate the gateway with the command in section 1.
To use files from another location, change the `file:` paths of `gateway_tls_cert` and `gateway_tls_key` in `compose.yaml`.

## 4. Reverse-proxy offload (supported alternative)

An external reverse proxy or load balancer may terminate TLS for the public name. The gateway still expects TLS,
so the proxy must connect to it over HTTPS (re-encryption) and verify its certificate against the CA that signed `server.crt`.
The proxy must send these headers to the gateway:

- `Host`: the public host name.
- `X-Forwarded-Proto: https`. Overwrite any value from the client.

The gateway sets `X-Forwarded-Proto` to `https` itself. It also overwrites `X-Forwarded-For` with the address of its direct
peer, so the client chain sent by the proxy is not passed on. The API reads `X-Forwarded-For` only from the gateway's
address (`SAFE_TRUSTED_PROXIES`). With offload, the API therefore sees the proxy's address as the client, and the login
rate limits and demo sign-in use that address. Passing the client chain through needs a gateway change that is not made here.

## 5. Not covered

- Client certificates (mTLS).
- HSTS preload and `includeSubDomains`. The header is `max-age=31536000` only.
- OCSP stapling.
- Let's Encrypt or other public ACME issuance. Internal names cannot use it.
- An HTTP-to-HTTPS redirect. Port 8080 is not published.
- Trust in browser stores other than the operating-system store described above.
- The gateway-to-API hop, which is still plain HTTP on the internal `edge` network.
- Certificate expiry monitoring and rotation.
