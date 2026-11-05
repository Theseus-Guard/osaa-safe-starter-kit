# Concepts and glossary

Read this page first if you have not used SAFE-Starter-Kit before. It explains the terms the screens and the other pages use. The steps are in [Workflows](./workflows.md), and the screens are in [Screens](./screens.md).

The application follows the SAFE workflow for AI incidents and near misses. The project is developed to conform to SAFE v1.0 and is not certified or endorsed by the Open Secure AI Alliance. The [Overview](../../README.md) has the status.

## The main objects

- **Case.** One incident or near miss. A case holds its record, its deadline tasks, its evidence, its reviews, its links to other cases, and its disclosure packages. A closed case cannot be changed, except that it can be reopened, and evidence can still be uploaded to it.
- **Reporter channel.** A case opened by a reporter. The reporter sees its own cases. A triager sees the case before it is assigned, and never sees the reporter identity or the reporter contact.
- **Assignment.** An investigator or a reviewer assigned to a case. Only assigned people can work on the case's evidence and reviews.
- **Case link.** A typed link between two cases: duplicate, related, or root and sub-case. Each link has a reason. A duplicate link closes the duplicate case. A root and sub-case link lets a reader of the sub-case read the root case, read only.
- **Evidence.** Files attached to a case. The application keeps a custody log of every upload, download and check. Each item has a TLP label that limits who may see it.
- **Catalog.** A versioned list of defences, with their verification history. It is separate from the cases.

## Time and deadlines

- **Awareness time.** When the organisation became aware of the incident. The case has one, entered when the case is opened.
- **Awareness event.** A later moment that the operator records on the case. Once recorded, it cannot be edited or removed.
- **Deadline start.** The earliest of the awareness time and the awareness events. A later event never moves the start.
- **Deadline task.** A task that the application sets from the start. Each task has a status: open, done, or not applicable. A task that is still open after its due time is overdue.
- **Business day.** Monday to Friday. The application has no holiday calendar yet, so the business-day deadlines are provisional.

The deadline tasks that the application sets, and their due times, are listed in [Workflows, section 1](./workflows.md#1-open-a-case).

## Assessment of the case

- **Impacts and classification.** The triager records them on the case. They describe what happened and how it is classified.
- **Risk score.** The case list shows it as a value and a level, or as "Not assessed" when no score exists yet.
- **Legal duties.** Obligations that the organisation has to meet for the case. The triager records them.
- **Weekly update.** A task opened while a significant risk is open. It is due 7 days after the risk was recorded.

## Review

- **Review round.** One assessment of a case by an assigned reviewer. A round has eleven layers, and each layer gets an outcome: Pass, Fail, Insufficient evidence, or Not applicable.
- **Conflict, recusal and appeal.** A reviewer who has a conflict declares it, and a reviewer who withdraws names a replacement. Any role that can read the case, except the disclosure approver, may file an appeal. All three are audited.

## Disclosure and external notices

- **Disclosure package.** Redacted copies of case evidence, prepared for one recipient and one purpose. It has a template and a digest, which is a fingerprint of its content.
- **Version.** A package changes only by a new version. Earlier versions are kept as they were written.
- **Preview.** The structure of a version as the reader is allowed to see it. It shows the recipient, the purpose, the digest, and the evidence items as derivatives.
- **Approval.** A disclosure approver confirms the recipient, the purpose and the digest of the latest version. The preparer of a version cannot approve it.
- **External notice.** A record that a person sent the approved version outside the organisation. The application does not send anything. A notice with no receipt stays unknown, and is never shown as delivered.
- **Export.** A JSON file of an approved version. An approved version can be exported; a version that was never approved cannot.

The disclosure steps are in [Workflows, sections 8 to 11](./workflows.md#8-prepare-a-disclosure-version-api-only).

## Backup approval

When the approver who should send an urgent notice cannot do it, the preparer opens an escalation, with the reason (unreachable, or a conflict of interest), and names a backup approver from a pool. The pool belongs to one case, holds at least two people, and is set by an institution owner. A backup may decide only after the waiting time has passed. The waiting time is 4 hours by default, and an institution owner can change it, with a reason.

An escalation records the **missing facts**: the facts that the preparer records as missing for the version. The Disclosure tab shows them, read only. Recipient reviews and package reviews are recorded against a version as well. None of these records sends a notice or changes a deadline.

Backup approval is available through the API only. See [Workflows, section 14](./workflows.md#14-backup-approval-api-only).

## Roles

Each account has one role. The role decides what the account can see and do. See [Roles and permissions](./roles-and-permissions.md) for the table, the fixed rules, and the suggested holder of each role.

- **Institution owner.** A role for the person who runs the external notice process. It sets the backup pool of a case and the waiting time. It cannot approve, prepare or triage.
- **Administrator.** A role for the platform. It manages accounts and roles, and it has no access to case content.

## Words you will see in the interface

| Word | Meaning |
|---|---|
| Open, Done, Not applicable | The three statuses of a deadline task |
| Overdue | A deadline task that is still open after its due time |
| Restricted | An action or a name that your role may not see. The application shows the event, without the details |
| Withheld | An evidence item outside your TLP audience, shown in a preview without its content |
| Pending password change | An account that must choose a new password at its next sign-in |
