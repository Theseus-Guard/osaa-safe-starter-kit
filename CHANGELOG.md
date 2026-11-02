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
- Case review: an assigned reviewer records an outcome (pass, fail, insufficient evidence, not applicable) with an optional note. Every review of a case is listed for callers who can read the case. Review notes are shown only to callers with `cases:read_all`; other callers see the outcome and time.
- Evidence: upload, download, and a custody record for each event. Files are encrypted with AES-256-GCM, keys come from a file, and uploads have a size limit and a media-type allowlist. New permission `evidence:read`; `evidence:write` is needed to upload.
- Evidence verification: an upload becomes available only after the stored object is read back and checked with the download rules. The worker marks uploads that stay pending for 30 minutes as failed.
- Case import from a versioned JSON file (`import_format_version` 1), through the API and `safe-kit case import`. Each record reports imported, duplicate, conflict, rejected, or failed.
- Synthetic demo dataset (`python -m safe_kit.demo_seed seed`), refused unless `SAFE_MODE=demo` and `SAFE_DATASET=synthetic-demo`. It is idempotent. Each demo role (administrator, reporter, triager, investigator, reviewer, disclosure approver, auditor) has its own random credential, stored only as a hash. The plain value is written once to a file per role in the directory named by `SAFE_DEMO_CREDENTIAL_DIR` (mode 0600). The demo administrator's file is still the one named by `SAFE_DEMO_CREDENTIAL_FILE` when that is set. Demo sign-in works only on loopback, in demo mode, with a demo username and its credential. The login screen lists the demo roles in demo mode. The seeded cases are assigned to the investigator and the reviewer, so those roles see them; the triager and the approver read every case by their real permissions.
- Existing demo databases (seeded before per-role credentials) get the new credentials once, with the directory set: `SAFE_DEMO_CREDENTIAL_DIR=/path/to/dir python -m safe_kit.demo_seed upgrade`. The administrator's credential does not change.
- Optional `SAFE_TRUSTED_PROXIES` (IP addresses or CIDR ranges). The API reads `X-Forwarded-For` only from these proxy addresses, and the gateway overwrites that header with the address of its peer. The login rate limits and demo sign-in use the resolved client address.
- Profile fields (first name, last name, title, email); the top bar shows the full name and username.
- Test suites for the backend and the frontend, with coverage measured by the commands in the README.
- Case record: awareness events (incident, near miss, or blocked attempt, with the awareness kind and basis), impacts for each of the six plan areas (an area without a row is reported as not assessed), legal duties with their own due dates that never change deadlines, and a classification stored as versions with the five R01 trigger values. Each classification update must give the expected version. Migrations 0023 and 0024. Writes need `cases:triage`.
- Preliminary report for a case, which lists the missing fields instead of refusing. It needs `cases:read_all`.
- Free text in case records is returned only to callers with `cases:read_all`, as close reasons and review notes are. Audit rows keep only the length of the text.
- Reporter channel: `POST /api/v1/reports` opens a case for a signed-in reporter account and stores an optional follow-up contact (up to 200 characters) apart from the case content (migration 0021). The reporter account is never returned, and the case history hides the actor of the case-creation entry for every caller.
- Reporter contact: readable only with the new permission `reporter_contact:read`, which no role holds by default (migration 0022). Every read is audited. Reporter-channel cases are hidden from `cases:read_all` callers until the case has an active assignment. An administrator cannot reset the password of a reporter account.
- Case search: `GET /api/v1/cases` takes `q`, which matches the reference (partial, case-insensitive) or the start of the case ID. The header search uses it. Search only narrows the cases the caller can already see.
- Case list: `GET /api/v1/cases` takes `status` (`open` or `closed`), each case shows its next open deadline and its last update time, and the case list screen and `safe-kit case list` show both.
- Case visibility is checked in one place for case reads, lists, history, assignments, reviews, and evidence. An import response names an existing case only when the caller can see it.
- Sessions end after 60 minutes without activity and at most 12 hours after login. The browser renews the token pair on the first request made more than five minutes after the last renewal; refresh tokens rotate on every renewal.
- Saving a case classification requires `cases:read_all` as well as `cases:triage`. Without it the save is refused, because it would clear the free-text fields the caller cannot see.
- Rate limits at the gateway: login is limited to 10 requests per minute per client, with a burst of 5, and the API to 30 requests per second, with a burst of 60. Excess requests get `429`. The API also keeps login counters in PostgreSQL, per username and per client, over a 15-minute window.
- Validation errors (`422`) no longer repeat the rejected input, so a submitted password is not echoed back.
- Case record screens on the case detail page: awareness events, impacts, legal duties, classification with version checks, and the preliminary report.
- Reporter contact: the contact is read on request only, and each read is audited. The button sits under the note that reading is recorded in the audit log.
- Case assignments: a picker lists the users who may be assigned, from the new `GET /api/v1/cases/{case_id}/assignable-users`, which requires `cases:assign`.
- CLI case updates: `case close`, `case reopen`, `case assign`, `case unassign`, `case review record`, `case classification set`, `case impact set`, `case legal-duty add` and `update`, `case awareness add`. Each command logs in with a password prompt (or stdin), keeps the tokens in memory for that command, and ends the session afterwards. Nothing is written to disk.
- The inactivity watch signs out after 60 minutes without an API request and warns five minutes before. Mouse and keyboard use do not count as activity.
- The admin role cannot hold case or reporter contact permissions. `PUT /api/v1/rbac/roles/admin/permissions` answers `409` for such a set.
- No role can be granted `reporter_contact:read` through the role matrix. The matrix answers `409` with `reporter_contact_not_grantable`.
- Reporter contact can be read while the case is open, or within 72 hours after it was closed. Later reads are refused with `409` `reporter_contact_window_closed`, and the refused read is not audited.
- Assignees are chosen by the `cases:assignable` permission, granted to investigator and reviewer by migration 0026. `GET /api/v1/cases/{case_id}/assignable-users` lists them for users with `cases:assign`.
- Classification version conflicts answer `409` with `classification_version_conflict` and the current version, so clients do not parse text.
- SAFE deadlines start from the earlier of the case awareness time and any operator awareness event. A later operator event never delays them.
- A `reporting_owner` role holds `reporter_contact:read` and `cases:read_all` (migration 0029). It is the only role that can hold the contact grant. Its permission set is fixed: the role matrix answers `409` `reporting_owner_fixed` for any change to it. Admin can assign the role, and each assignment is audited. The role reads the content of every case, not only the contact, because the contact route needs `cases:read_all` to reach a case without an assignee.
- CLI read commands for case records: `case classification show`, `case impact list`, `case legal-duty list`, `case awareness list`, and `case report show` (needs `cases:read_all`).
- Classification has an explicit `none_applies` trigger (migration 0027). It cannot be combined with other triggers.
- The internal incident option works per task. Each external notice task is marked not applicable on its own, with a reason, through the deadline update. The case-wide switch is gone: a classification that sends `internal_incident_option` is refused with `422`. Migration 0037 gives each case that had the switch the same per-task exceptions, with the reason recorded as migrated. `POST /api/v1/cases/{case_id}/deadlines/{task_id}/restore` turns one exception off and returns the task to its previous status. Due dates do not change.
- An appeal names no reviewer. The system names the independent reviewer: the eligible reviewer with the fewest open appeals, then the lowest user ID. A request that names a reviewer is refused with `422`. When no reviewer is eligible, the appeal is refused with `409` `no_eligible_reviewer` and nothing is stored.
- Linked cases: two cases can be linked as a record only (migration 0028). Links change no task, deadline or status. Links are added, listed and removed from the case detail page and the CLI.
- Case risk: each case has impact values C (Confidentiality), B (Integrity) and A (Availability), each 1 to 5, stored as versions by holders of `cases:triage` (migration 0038). The risk score is their mean to one decimal place and shows Not assessed until all three are set. The case list, the case detail and the CLI show it.
- Catalog: versioned entries with versions, corrections, disputes, withdrawals and supersessions (the old version is kept), recommendations (a suggestion with no result), and verification runs that are attached to an entry. Only a passing run verifies it. A restricted reference is returned only with `catalog:read_restricted`. Recommendations have their own list endpoint. The catalog is on the Catalog page of the web UI and in the CLI as `catalog list`, `show`, `add`, `version`, `correct`, `dispute`, `withdraw`, `supersede`, `verify`, and `catalog recommendation list` and `add`.
- Redaction and TLP: a text/plain evidence item can be redacted into a new item with its own ID, a manifest of the removed line ranges and reasons, and a TLP label that is not less restrictive than the original's. Claims that cite the original are assessed as insufficient evidence on the copy. Evidence reads show the TLP label, and a TLP-hidden item answers the same not-found as a missing one. The CLI has `evidence show`, `derivatives`, `redact` and `show-redaction`, and the case evidence list has a details view with the copies, the manifest and the claims.
- Retention period: `SAFE_RETENTION_YEARS` (a whole number from 1 to 30, required by the API and the worker, set to 5 in `compose.yaml`) is shown for every table in the privacy inventory. The period is provisional and waiting for legal review (decision D-09). Nothing is deleted when it ends.

### Not yet implemented
- Deletion at the end of the retention period. The period is shown only, until legal review is complete.
- A production certificate, OIDC/SSO, encryption at rest for the database volume, and backup and restore procedures.
- The disclosure workflow and the later review framework (F2), including export and recipient packages.
- Anonymous reporter intake. The current reporter channel requires a reporter account. The Vault Transit key provider and object storage for evidence are also not built.
- Verification on Ubuntu 24.04 and 26.04 LTS, which is planned after all other work.
