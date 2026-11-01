# Changelog

All notable changes to SAFE-Starter-Kit are documented here.

Every release entry states the **SAFE compatibility baseline**: the SAFE version and commit the release was checked against. A newer SAFE version never makes a release compatible with that version automatically. Compatibility is only claimed for the baseline written in the entry, and it does not mean certification or legal compliance.

## Unreleased

**SAFE compatibility baseline:** SAFE v1.0 (2026-10-21), wg-safe commit `08c0d6a11163d10ac878a15dc9f53b5b322fd7cb`. Not yet released.

### Added
- Single-organization self-hosted stack (Docker Compose): PostgreSQL, one-shot migrations, API, worker, nginx gateway, container CLI.
- Username and password login with Bearer JWT, refresh-token rotation, logout, rate limiting, forced password change.
- First admin created once from the command line; first admin is protected and cannot be disabled, deleted, or demoted.
- Admin user management: list, role and status changes, soft delete, password reset. Editable role-permission matrix.
- Soft delete for every table, with `created_at`, `updated_at`, `deleted_at`, `is_deleted`.
- Audit log (append-only) and transactional outbox with a worker.
- Minimal case domain: create a case from an awareness time (t0) and calculate SAFE notification deadlines.
- Personal data inventory (metadata only) with JSON and CSV export.
- System timezone chosen at first admin login; changes are confirmed and audited.
- React and TypeScript web UI, container CLI with case commands.

### Not yet implemented
- TLS, OIDC/SSO, evidence storage, disclosure workflow, backup and restore procedures, encryption at rest.
- Verification on Ubuntu 24.04 and 26.04 LTS.
