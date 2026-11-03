# Workflows

This page lists the main workflows in the order a case usually follows. Each step names the screen or the API route that performs it. The permissions are the default ones (see [Who uses it](./index.md#who-uses-it)). The [Screens](./screens.md) page describes each screen in full.

## 1. Open a case

A reporter (a role with `cases:submit`) opens a case from **Cases**, with **New case**. The dialog asks for:

- **Title** (required, up to 200 characters)
- **Awareness date and time**, in the system timezone (the time when the reporting member became aware of the facts)
- **Contact for follow-up** (optional, up to 200 characters)

The application stores the contact apart from the case content. It is never shown in the case; only the reporting owner can read it, and every read is audited. The case opens with the SAFE deadlines calculated from the awareness time (see below).

A case opened this way is a reporter-channel case. A triager sees it before it has an assignment, so the triager can assign an investigator or reviewer. The triager never sees the reporter account or the contact. The reporter sees its own cases.

**Deadlines.** The deadline start is the earliest of the case's awareness time and the operator awareness events recorded on the case. It never moves later. The application then sets these SAFE deadline tasks:

| Task | Due |
|---|---|
| Affected customers | 72 hours after the start |
| Preliminary SAFE report | 4 business days after the start, on the provisional calendar |
| Broader advisory | 14 days after the start. It starts as not applicable, because its condition is not recorded yet. A triager sets it to open once the condition holds |
| Preliminary public report | 30 days after the start |
| Remediation status | 90 days after the start |

The business-day calendar counts Monday to Friday. It has no holidays yet, so it is provisional. A weekly update task is opened while a significant risk (an impact of high or critical severity) is open. It is due 7 days after the risk was recorded. Generating a weekly update does not move this due date.

## 2. Assign investigators and reviewers

A triager (`cases:assign`) opens the case, goes to the **Overview** tab and uses **Assignments**. The assignee must be an active user whose role can be assigned (investigator and reviewer by default). Reporters cannot be assigned. The triager can remove an assignment too. Each change is audited (`case.assignment_added`, `case.assignment_removed`). A closed case cannot be changed.

## 3. Record the case record

A triager (`cases:triage`) records, on the **Overview** and **Deadlines** tabs:

- the impact values (C, B and A, each from 1 to 5, or not set). The risk score is the mean of the three values, rounded to one decimal place. Each save stores a new version of the impact values;
- the classification, with its triggers and the next update due;
- the awareness events, with the time each one occurred on. An awareness event cannot be edited or removed once recorded;
- the legal duties, with their jurisdiction and statute or contract clause;
- links to other cases, by case ID.

The application does not compute legal notification duties. They run on their own clocks and are recorded by hand (see [Not built yet](#not-built-yet)).

## 4. Work on the deadlines

Deadline tasks have the status open, done or not applicable.

- An investigator or reviewer assigned to the case, or a triager, can set a task to done or open.
- Only a triager can mark a task not applicable, with a reason, and restore it.
- A task that is still open after its due time counts as overdue. The Overview counts it under **My overdue deadlines**, and the case list shows **Overdue** next to the next deadline when it has passed.

Each status change is audited (`case.deadline_updated`), with the old and the new status.

## 5. Record a review

Only an assigned reviewer (`reviews:write`) can open a review round. The round has eleven layers, and every layer starts as not set.

1. Open the round on the **Reviews** tab with **Open review round**.
2. For each layer, choose the outcome (Pass, Fail, Insufficient evidence or Not applicable), then **Save layer**.
   - A reason is required for every outcome except Pass.
   - A Pass needs at least one evidence item of the same case.
   - A save checks the revision that was read. If another user changed the layer first, the save is refused and nothing changes. Reload the layer and save again.
3. If a reviewer has a conflict with the round, the reviewer declares it. A reviewer who withdraws names a replacement. Any role that can read the case, except the disclosure approver, can file an appeal.

Each round and each layer change is audited (`case.review_round_opened`, `case.review_layer_set`), and so are the conflicts, recusals and appeals.

## 6. Close and reopen the case

A triager with `cases:close` closes the case with a reason. The application refuses the closure while any of these is true, and lists the reasons:

- no review layer has an outcome yet;
- a deadline task is neither done nor not applicable;
- an external notice has no receipt, and no follow-up has been recorded for it.

Reopening needs a reason and `cases:close`. Both changes are audited (`case.closed`, `case.reopened`).

## 7. Handle evidence

An investigator (`evidence:write`) uploads a file on the **Evidence** tab. The accepted types are PDF, PNG, JPEG, plain text and JSON. Every upload, download and integrity check is written to the custody log of the item. The downloads and uploads are audited (`evidence.uploaded`, `evidence.downloaded`).

A role with both `evidence:read` and `evidence:write` can make a redacted copy of a text original. The redacted copy is a separate evidence item, and the change is audited (`evidence.redacted`).

## 8. Prepare a disclosure version (API only)

A disclosure package holds redacted derivatives of the case evidence, with a recipient, a purpose, a template and a digest. A new package starts at version 1. A new version of an existing package is the next version.

There is no button for this in the application. A role with `disclosures:prepare` (triager by default) creates a version through the API:

- `POST /api/v1/cases/{case_id}/disclosures` creates version 1 of a new package, or the next version when the body names the `package_id`. The body is the `VersionRequest` model in `backend/src/safe_kit/disclosure_api.py`. It needs an `Idempotency-Key` header.
- The Disclosure tab lists the versions and their previews.

A new version does not change the earlier ones. Versions are kept as they were written.

## 9. Get the approval

The approver (`disclosures:approve`) opens the **Disclosure** tab, selects the latest version and reads its preview.

1. **Approve this version** asks for the **Recipient**, the **Approved recipient reference** (an identifier kept outside the application), the **Purpose** and the **Digest**. Type the digest exactly as the preview shows it.
2. Submit. The approval covers this version only. A later version needs its own approval.

A preparer cannot approve a version they prepared. The approval is audited (`disclosure.approved`).

## 10. Record an external notice

After approval, a person who may record notices (disclosures:approve, or disclosures:prepare with a reason) records that the approved version was sent. The application does not send anything. The notice records a send that a person made.

1. On the **Disclosure** tab, choose **Record notice**. Fill in the **Channel** (and **Describe the channel** for another channel), the **Recipient reference** and **Sent on**.
2. The recipient reference must match the one the approver confirmed. Otherwise the notice is refused.
3. A second notice for the same version needs a reason (**Reason for a second notice**).
4. Record what happened next with **Record receipt** (the receipt date, or the reason no receipt is known), **Mark follow-up** and **Complete follow-up**, or **Cancel notice**. When no receipt is known, a follow-up is recorded with the notice.

Notice changes are audited (`disclosure.notice_recorded`, `disclosure.notice_state_changed`).

## 11. Export

- **Disclosure version**: an approved version has **Export JSON** on the Disclosure tab. The file is `disclosure-version-<number>.json`, in the format of `schemas/disclosure-export.v0.json`. An older approved version is exported with the status `superseded`. Each export is audited (`disclosure.exported`).
- **Public catalog**: `GET /api/v1/catalog/export`, or `safe-kit catalog export` in the CLI. It needs `catalog:read`, and each export is audited (`catalog.exported`). The catalog export has no button in the application.
- **Privacy inventory**: **Export JSON** and **Export CSV** on the Data privacy page (administrators only).

## 12. Maintain the catalog

A reviewer (`catalog:write`) adds entries and versions on the **Catalog** page, and records corrections, disputes, withdrawals, supersessions, recommendations and verification runs on the entry page. An entry shows as verified only after a passing verification run. Earlier versions are kept, and withdrawn or superseded entries keep their history.

## 13. Administer accounts, roles and the timezone

Administrators use the **Users**, **Roles and permissions** and **Settings** pages. These pages are described in [Screens](./screens.md). Each change is audited.

## Not built yet

These features are not in the application yet. The screens and the API do not offer them.

- Escalation and alert records.
- Recipient review records and package review records.
- A backup approver for approvals.
- The 30-day analysis task. Only the preliminary public report deadline (30 days) exists.
- An official holiday calendar. Business days exclude weekends only.
- Sending a disclosure. Notices record a send made by a person.
- Importing a disclosure export.
- Single sign-on, a production certificate, and backup and restore procedures.
