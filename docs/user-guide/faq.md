# FAQ

## Why does the case list show no cases?

The list shows only the cases your role may see:

- A reporter sees only the cases it opened. With none, the list says "You have not opened any cases yet."
- An investigator or a reviewer sees only the cases it is assigned to.
- A triager sees every case, including a reporter-channel case that has no assignment yet, so it can assign one. It never sees the reporter or the reporter contact.
- A disclosure approver does not see the case list. It sees only the disclosure packet it is assigned to approve. See [Roles and permissions](./roles-and-permissions.md).
- The administrator and auditor roles have no case access. The Cases page says so. An institution owner sees every case, so it can assign the backup approvers of a case.
- A search or a status filter narrows the list. The list then says "No cases match this search." or "No cases have this status."

A case opened through the reporter channel is visible to a triager before it is assigned, so the triager can make the first assignment. See [Workflows](./workflows.md#2-assign-investigators-and-reviewers).

## What does Overdue mean?

- In the case list, **Overdue** next to the next deadline means that the earliest open deadline task of the case is past its due time.
- On the Overview, **My overdue deadlines** counts the open tasks of your open cases whose due time has passed.

A task that is done or not applicable is never overdue. The times are shown in the system timezone.

## Why does an edit say that the record changed?

Someone else saved a change after you opened the record. The application checks the revision you read before it saves, and it refuses your save so that nothing is overwritten. The message depends on the record:

- "This record was changed by someone else. Reload it, check the current revision and save again." (for example a review layer)
- "The classification was changed by someone else. Reload it, check the current version and save again."
- "The impact values were changed by someone else. Reload them, check the current version and save again."
- "The item changed, so nothing was saved. Reload it, check the current version and save again."

Reload the record, check the current values, and make your change again.

## Where is the audit log?

- For one case, open the **Activity** tab. It lists the actions on that case. The actor and the details appear only when your role has full access to the case (`cases:read_all`). Otherwise they show as Restricted.
- On the Overview, the **Audit actions** chart shows counts per day for the auditor and administrator roles. It shows no names.
- The application has no screen that lists the whole audit log. Changes to the system timezone are audited too, and they appear in the same log.

## Why can't I see a button or a tab?

Each control appears only when the role has the permission for it. Some examples:

- The **Evidence** tab needs `evidence:read` or `evidence:write`.
- The **Disclosure** tab needs `disclosures:read`. The triager and the disclosure approver have it by default.
- **Catalog** in the navigation needs `catalog:read`.
- **Users**, **Data privacy**, **Roles and permissions** and **Settings** are for administrators only.
- A closed case is read only. Its change buttons are disabled.

The server checks every change again, whatever the screen shows.

## Why does the Overview say "Your role has no overview sections"?

The Overview shows only the sections that match the role. The reporting owner has none of them. The cards and charts for each role are listed in [Screens](./screens.md#overview).

## Why can't I close the case?

The application lists the reasons. A case can be closed only when:

- at least one review layer has an outcome;
- every deadline task is done or not applicable;
- every external notice that has no receipt has a follow-up recorded.

## Why are my deadlines not at the time I expected?

The deadlines run from the start time, which is the earliest of the case's awareness time and the operator awareness events recorded on the case. A later event never moves the start. The business-day deadline counts weekdays only, because no official holiday calendar exists yet.

## Why is the Approve button disabled?

The application approves only the latest version of a disclosure package. Approving an earlier version is not possible; approve the latest one. A preparer cannot approve a version they prepared. The approval also needs the `disclosures:approve` permission and the exact digest shown in the preview.

## Why is a notice refused?

A notice must name the recipient reference that the approver confirmed for the version. A different reference is refused with `recipient_mismatch`. A second notice for the same version also needs a reason.

## Why can't I see the reporter contact?

Only the reporting owner has the permission. The contact can be read while the case is open, and for 72 hours after the case was closed. Each read is audited.

## Why must I change my password?

A new account must change its password at its first sign-in. An administrator's reset does the same. A password also has to be changed after 180 days. A new password cannot match one of the last three passwords.

## Why was I signed out?

A session ends after 60 minutes without activity, and in any case 12 hours after the sign-in. Changing your password ends all your sessions too.

## Why does the sign-in page ask for a demo role?

Only the demo does. The stack from `compose.yaml` runs in production mode, and its sign-in page takes a username and password. The demo is started with `scripts/demo.sh`, which adds the overlay `compose.demo.yaml`; with the overlay, the sign-in page offers demo sign-in only. See the [Demo](./demo.md) page.

## Why can't I change the password of a demo account?

Demo accounts sign in only with their demo credential, which is kept separately from the account password. The Account screen refuses a password change for them with "Demo accounts sign in with their demo credential, so their password cannot be changed here." Sign in again with the credential from the demo's credential folder.

## Why does the export of a version fail?

Only an approved version can be exported. The application refuses an export in these cases:

- The version is not approved. Approve the latest version first.
- The stored content does not match its digest. Nothing is exported. Tell an administrator.
- The approval does not match the version. Tell an administrator.
- A CLEAR item has no release approval. Record the release approval first.
- The release has no trace record. Tell an administrator.

A successful export is written to the audit log without its content. A refused export is recorded as a refused release attempt.

## Why does the Restricted label appear?

The label replaces an actor name or a detail that your role may not see. The event itself is still shown. See [Roles and permissions](./roles-and-permissions.md) for what each role can see.

## Why does the Missing facts section say no escalation is recorded?

The section lists the facts that the preparer recorded as missing when a version was escalated to a backup approver. If no escalation was opened for the version, it stays empty. Opening and deciding an escalation is an API-only step; see [Workflows, section 14](./workflows.md#14-backup-approval-api-only).

## Why is there no button to set the backup approvers?

The backup pool, the escalation, the backup decision and the waiting time have no screen yet. They are API calls for an institution owner or the preparer. See [Workflows, section 14](./workflows.md#14-backup-approval-api-only).

## Running the installation

The stack does not start, the browser shows a certificate warning, or a command fails? These problems belong to the person who runs the installation, not to the people who use it. The [Operator guide](./operator-guide.md#troubleshooting) lists what to check for each symptom. In short:

- Start with `docker compose ps`, then the logs of the service that is not healthy.
- The browser warns about the certificate until you trust the local CA. See [gateway/README-TLS.md](../../gateway/README-TLS.md).
- A second stack on port 8443 blocks the first one. Stop it first.
