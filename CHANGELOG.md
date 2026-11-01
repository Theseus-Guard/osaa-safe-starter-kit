# Changelog

All notable changes to SAFE-Starter-Kit are documented here.

Every release entry states the **SAFE compatibility baseline**: the SAFE version and commit the release was checked against. A newer SAFE version never makes a release compatible with that version automatically. Compatibility is only claimed for the baseline written in the entry, and it does not mean certification or legal compliance.

## Unreleased

**SAFE compatibility baseline:** SAFE v1.0 (2026-10-21), wg-safe commit `08c0d6a11163d10ac878a15dc9f53b5b322fd7cb`. Not yet released.

### Added
- Single-organization self-hosted stack (Docker Compose): PostgreSQL, one-shot migrations, API, worker, nginx gateway, container CLI.
- Username and password login with Bearer JWT, refresh-token rotation, logout, rate limiting, forced password change.
- Password policy: passwords expire after 180 days, and a new password may not reuse the last three passwords or any temporary password issued to the account.
- First admin created once from the command line; first admin is protected and cannot be disabled, deleted, or demoted.
- Admin user management: list, role and status changes, soft delete, password reset. Editable role-permission matrix.
- Soft delete for every table, with `created_at`, `updated_at`, `deleted_at`, `is_deleted`.
- Audit log (append-only) and transactional outbox with a worker.
- Minimal case domain: create a case from an awareness time (t0) and calculate SAFE notification deadlines.
- Personal data inventory (metadata only) with JSON and CSV export.
- System timezone chosen at first admin login; changes are confirmed and audited.
- React and TypeScript web UI, container CLI with case commands.
- HTTPS-only gateway with a development certificate authority (`scripts/make-dev-cert.sh`) and HSTS.
- Case lifecycle: assignment, deadline status per task, closing and reopening with stored reasons, and case history. Reasons and evidence events are shown only to roles that hold the matching permission.
- Case review: an assigned reviewer records an outcome (pass, fail, insufficient evidence, not applicable) with an optional note. Reviews from earlier sessions are not listed yet.
- Evidence: upload, download, and a custody record for each event. Files are encrypted with AES-256-GCM, keys come from a file, and uploads have a size limit and a media-type allowlist. New permission `evidence:read`; `evidence:write` is needed to upload.
- Case import from a versioned JSON file (`import_format_version` 1), through the API and `safe-kit case import`. Each record reports imported, duplicate, conflict, rejected, or failed.
- Profile fields (first name, last name, title, email); the top bar shows the full name and username.
- Test suites for the backend and the frontend, with coverage measured by the commands in the README.

### Not yet implemented
- A production certificate, OIDC/SSO, encryption at rest for the database volume, and backup and restore procedures.
- The disclosure workflow and the later review framework (F2), including export and recipient packages.
- The independent reporter channel, the Vault Transit key provider, and object storage for evidence.
- Verification on Ubuntu 24.04 and 26.04 LTS, which is planned after all other work.
