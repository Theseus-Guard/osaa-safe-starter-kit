# Operator guide

This page is for the person who runs the installation: it starts and stops the stack, keeps the secrets, runs the upgrades, and uses the operator commands. The steps for a first start are in [Quick start](./quick-start.md). The rules for the people who use the application are in [Roles and permissions](./roles-and-permissions.md).

Read the limits first. The stack is for local use and evaluation. It is not yet a production system: see [What is not built](#what-is-not-built).

## What runs

The stack is defined in `compose.yaml`:

| Service | What it does |
|---|---|
| `db` | PostgreSQL. Its data is in the `db-data` volume |
| `db-roles` | Creates the database roles that the application uses, once |
| `migrate` | Applies the database migrations. It runs at each start, and then stops |
| `api` | The backend. It serves `/api/v1` |
| `worker` | Runs the background tasks, such as weekly updates |
| `gateway` | The only service with a host port. It serves HTTPS on `127.0.0.1:8443` and forwards to the API |
| `cli` | The container CLI. It starts only with `--profile cli` |

Only the gateway is attached to the network that the host can reach. Every other service sits on an internal network with no route out, so the API and the worker cannot reach the internet.

Evidence files are in the `evidence-data` volume. The database and the evidence volumes hold all the case data.

## Secrets

`scripts/init-secrets.sh` creates the secrets in `secrets/`, once. It never overwrites an existing file. The files are:

- `db_password`, `db_migrator_password`, `db_app_password`, `db_worker_password`: the database passwords for each role.
- `jwt_signing_key`: signs the access tokens.
- `evidence_kek`: the key that protects the evidence files.
- `tls/`: the development certificate and its CA key.

Rules:

- Keep `secrets/` out of the repository and out of shared folders. It is git-ignored.
- In production mode the API and the worker refuse to start unless the gateway's certificate and key (`secrets/tls/server.crt` and `server.key`) are mounted and readable. Compose sets their paths in `SAFE_TLS_CERT_FILE` and `SAFE_TLS_KEY_FILE`. The check does not read the key. Demo mode does not need them. See [gateway/README-TLS.md](../../gateway/README-TLS.md).
- Back up `evidence_kek` together with the evidence volume. Without the key, the evidence cannot be read. Store the backup in a place with the same access rules as the secrets.
- Do not change `jwt_signing_key` on a running stack unless you accept that every session ends.
- Plan a change to a database role password before you make it: the services read the secret when they start.

## Start, stop and check

```sh
docker compose up -d --build --wait     # start, and wait until the services are ready
docker compose ps                       # service states
docker compose logs --tail=100 api      # recent log lines of one service
docker compose down                     # stop; the volumes stay
```

Do not run `docker compose down -v` on an installation with data. The `-v` flag deletes the volumes.

## Upgrades

1. Take a backup of the database and the evidence volume. The application does not provide this; use your own PostgreSQL and volume tools.
2. Update the code, then run `docker compose up -d --build --wait`. The `migrate` service applies the new migrations.
3. Check `docker compose ps` and read `docker compose logs migrate`.

Migrations are forward only. A migration that has run cannot be undone by the application. A rollback means restoring the backup from step 1.

## Operator commands

The operator commands write to the database directly. Run them only inside the API container, and keep the container's access to people who are allowed to change accounts.

```sh
# The first administrator. This works once; it is refused when an administrator exists.
printf '%s' '<password>' | docker compose run --rm -T --no-deps api \
  python -m safe_kit.admin create-user --username <name> --role admin --email <address>

# Reset a password. The user must choose a new one at its next sign-in, and all its sessions end.
printf '%s' '<temporary password>' | docker compose run --rm -T --no-deps api \
  python -m safe_kit.admin reset-password --username <name>

# Give an active account the institution_owner role. A reason is required.
docker compose run --rm -T --no-deps api \
  python -m safe_kit.admin owner grant --username <name> --reason "<why>"

# Take the institution_owner role away, and give the account another role. A reason is required.
docker compose run --rm -T --no-deps api \
  python -m safe_kit.admin owner revoke --username <name> --role <role> --reason "<why>"
```

Notes:

- `reset-password` reads the new password from standard input when there is no terminal. The `owner` reason is at most 500 characters.
- An account that holds `institution_owner` cannot be changed or deleted through the application, and an administrator cannot reset its password. Use `owner revoke` first, then the usual account actions.
- The first administrator cannot be disabled, deleted, demoted, or given another role.
- Each operator change writes an audit row. An `owner` change also records its reason.

## Sessions and passwords

- Access tokens last 60 minutes. A session ends after 60 minutes without activity, and in any case 12 hours after the sign-in.
- Passwords must be at least 12 characters, and expire after 180 days. A new password may not match the last three.
- The login is rate limited in the gateway and in the database.

## Troubleshooting

| What you see | What to check |
|---|---|
| `docker compose up --wait` times out | `docker compose ps`, then the logs of `migrate` and `api` |
| The browser warns about the certificate | Trust `secrets/tls/ca.crt`, see [gateway/README-TLS.md](../../gateway/README-TLS.md) |
| Port 8443 is already in use | Another stack uses it. Stop that stack first |
| The first administrator is refused | An administrator already exists. Create the other accounts through the Users page |
| `owner grant` says the account is protected or inactive | The first administrator cannot receive the role. An inactive account must be enabled first, on the Users page |
| The container CLI returns an error | `docker compose --profile cli run --rm cli health` shows whether the API is ready |

## What is not built

These are not in the application yet. Plan for them before you use the installation for real incidents.

- A production certificate. The gateway uses a development certificate.
- Single sign-on (OIDC).
- Encryption at rest for the database volume.
- A Vault key provider for the evidence key. The key comes from the `evidence_kek` file.
- Backup and restore procedures. See [Upgrades](#upgrades) for the minimum you must do yourself.
- Sending disclosures and notices. A notice records a send that a person made.
- Screens for backup approval, escalation, and recipient and package reviews. They are API calls only (see the [Workflows](./workflows.md#14-backup-approval-api-only) page).
