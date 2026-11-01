# SAFE-Starter-Kit

A self-hosted starter kit for organizations that take part in the Shared AI Findings Exchange (SAFE). It supports the SAFE workflow for AI incidents and near misses: intake, deadline tracking, evidence preservation, review, controlled disclosure, and corrective actions.

> **Status: early development.** This is not a production system. It has HTTPS with a development certificate, but no production certificate, no OIDC/SSO, and no disclosure workflow yet. Do not use it with real incident data. See [ROADMAP.md](ROADMAP.md) for the phases.

## Independence and SAFE baseline

- SAFE-Starter-Kit is an independent open-source project. It is not an official Alliance product.
- Theseus Guard, Inc. is a member of the Linux Foundation and of the Open Secure AI Alliance (OSAA).
- It is developed to conform to **SAFE v1.0** (dated 2026-10-21), published by the SAFE Working Group in the [wg-safe repository](https://github.com/OpenSecureAIAlliance/wg-safe), at commit `08c0d6a11163d10ac878a15dc9f53b5b322fd7cb`.
- The SAFE specification is licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Its text is attributed to the Open Secure AI Alliance SAFE Working Group and wg-safe contributors. Any reuse keeps that attribution.
- This project is not yet endorsed, certified, or approved by the Open Secure AI Alliance or its committees.

Links: [Open Secure AI Alliance](https://secureaialliance.org/) · [wg-safe (official SAFE repository)](https://github.com/OpenSecureAIAlliance/wg-safe)

## How requirements change

- **SAFE requirements.** Our requirements follow SAFE v1.0. Structural or substantive changes to SAFE itself, and anything that is not a technical question about this project, must be raised as an issue in the [wg-safe repository](https://github.com/OpenSecureAIAlliance/wg-safe/issues), not here.
- **Changes to this project's SAFE baseline** are made only when the Working Group publishes a new SAFE version (for example, v1.1). We then update this project to that version.
- **Feature requests** opened in this repository do not change the SAFE baseline. We do not add capabilities in response to requests here.

## Repository layout

| Path | Contents |
|---|---|
| `backend/` | FastAPI API, worker, migrations (PostgreSQL), container CLI |
| `frontend/` | React + TypeScript single-page app (Vite, shadcn/ui, Tailwind CSS) |
| `gateway/` | nginx gateway that serves the app and proxies `/api` |
| `db/init/` | Database role setup (least privilege) |
| `schemas/` | Project-defined JSON schemas and examples (not official SAFE schemas) |
| `scripts/` | Local secret generation |
| `compose.yaml` | Docker Compose stack |

## Run it locally

Requirements: Docker with Compose v2.

```sh
./scripts/init-secrets.sh
sh scripts/make-dev-cert.sh
docker compose up -d --build --wait
```

The gateway serves HTTPS only, at `https://127.0.0.1:8443`, with a development certificate. Your browser will warn until you trust the local CA. See [gateway/README-TLS.md](gateway/README-TLS.md).

The first admin is created once from the command line. The password is read from stdin and must be at least 12 characters:

```sh
printf '%s' '<choose a password>' | docker compose run --rm -T --no-deps api \
  python -m safe_kit.admin create-user --username <name> --role admin
```

Log in, change the password when asked, then choose the system timezone. All times in the installation are shown in that timezone; stored times are always UTC.

Container CLI:

```sh
docker compose --profile cli run --rm cli health
```

## Tests

```sh
cd backend && uv run pytest
cd frontend && npm install && npm run build
```

## Test coverage

Measured on 2026-11-01 with the commands below. Figures are statement coverage of owned code; generated and third-party code is excluded.

| Part | Command | Tests | Coverage |
|---|---|---|---|
| Backend, without database | `scripts/test-backend.sh` | 919 passed, 224 skipped, 1 xfailed | 81% |
| Backend, with database | `scripts/test-backend.sh --db` | 1142 passed, 1 skipped, 1 xfailed | 97% |
| Frontend | `cd frontend && npm run test:coverage` | 347 passed | 72% statements |

The backend `--db` mode starts a throwaway PostgreSQL container and removes it afterwards. Database-backed tests are skipped unless `SAFE_TEST_DATABASE=1` is set, which the script does for you.

## Security notes

- The application never deletes rows; deletion is a soft delete. Physical removal is a manual database action.
- Every table has `created_at`, `updated_at`, `deleted_at`, and `is_deleted`.
- Passwords are stored as Argon2id hashes. Login is rate limited in the gateway and in PostgreSQL, and the gateway also limits API request rates.
- Passwords expire after 180 days. A new password may not match any of the account's last three passwords, nor any temporary password issued to the account.
- Access tokens last 60 minutes and are kept in memory in the browser. Refresh tokens are rotated on every use and also last 60 minutes, so a session ends after 60 minutes without activity. Whatever the activity, a session ends 12 hours after login. The browser renews the token pair before the access token expires.
- The application roles cannot change the schema. The bootstrap superuser password is random and not stored.
- Not yet implemented: a production certificate, OIDC/SSO, encryption at rest for the database volume, the disclosure workflow, a Vault Transit key provider, and backup/restore procedures.

## Versioning and SAFE compatibility

- Releases follow semantic versioning from v0.1.
- Each release is listed in [CHANGELOG.md](CHANGELOG.md) with the SAFE version and commit it was checked against.
- A new SAFE version (for example v1.1 or v1.2) does not make an existing release compatible with it. We state compatibility only for the baseline written in the release notes, and only after checking the release against that version.
- Neither a release nor this project is certified or declared compliant with SAFE.

## License

Apache License 2.0. See [LICENSE](LICENSE).
