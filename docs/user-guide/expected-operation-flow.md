# Expected operation flow

This page follows one AI finding from the first report to the closed case, and shows what each role does at each step. It explains why the roles are separate. For the exact permissions of each role, see [Roles and permissions](./roles-and-permissions.md). For the steps of each workflow, see [Workflows](./workflows.md).

This page describes the application as it is in version 0.3.0. Later versions, such as 0.4.0, may change a step or a role. Check the [Changelog](../../CHANGELOG.md) for the version you run.

The example is the demo case "Demo: tool call outside the approved scope". The demo data is synthetic. A real case follows the same steps.

## The roles in one view

| Role | Its job in the flow | What it cannot do |
|---|---|---|
| Reporter | Reports the finding. It sees only the cases it opened | See other cases |
| Triager | Checks the report, records the case record, assigns the investigator and the reviewer, and closes the case | Approve a disclosure, collect evidence |
| Investigator | Collects the evidence and works the deadline tasks of its case | Assign people, approve a disclosure |
| Reviewer | Records the review layers of the case | Assign people, approve a disclosure |
| Disclosure approver | Approves the disclosure packet, and records the external notices of the approved version | See the case, or any version it did not approve |
| Auditor | Reads the overview counts and the audit trail | Change anything, read case content |
| Reporting owner | Reads the reporter contact, so the organisation can answer the reporter | Change a case |
| Institution owner | Assigns the backup approvers of a case, and sets the backup waiting time | Approve, prepare, or triage |
| Administrator | Creates accounts, sets roles, resets passwords | Read or change any case, including the reporter contact |

## The flow

### 1. The finding is reported

A reporter opens a case with a title and the time the organisation became aware of the finding. In the demo, a model called an external tool that the approved scope does not allow. The case gets a reference and starts its deadlines.

- **Reporter:** opens the case. It can see only this case.
- **Triager:** sees every case, including reports that have no assignee yet. The reporter identity and contact stay hidden from it.

### 2. The triager checks the report and assigns the work

The triager records the impacts, the classification and the legal duties, and links the case to any related case. It assigns one investigator and one reviewer. The assigned people then see the case.

- **Triager:** records the case record and makes the assignments. It does not collect evidence or approve anything.
- **Investigator and reviewer:** see the case only after the assignment.

### 3. The evidence is collected

The investigator uploads the evidence: logs, the tool call record, and the approved scope document. Each upload is kept with its custody log. The original stays unchanged.

- **Investigator:** uploads the evidence and marks its deadline tasks as done.
- **Triager:** can read the evidence, but does not upload it.

### 4. The review is recorded

The reviewer opens a review round and records each review layer of the round. Where a reviewer has a conflict of interest, it declares it, and another reviewer takes over.

- **Reviewer:** records the review layers.
- **Investigator:** does not record review layers, so the investigation and its review are kept apart.

### 5. The deadlines are followed

The application calculates the SAFE deadlines from the awareness time. For this case they are the notice to affected customers, the broader advisory, the preliminary public report, the 30-day control-failure analysis, and the remediation status.

- **Triager:** watches the deadlines on the Deadlines tab, and is the only role that marks a task not applicable, with a reason.
- **Investigator:** marks its tasks as done.

### 6. The disclosure package is prepared

When the facts are known, a triager prepares a disclosure version. The version is a preview of the package. It is not yet approved, and it cannot be sent.

- **Triager:** prepares the version. It cannot approve the version it prepared.

### 7. The disclosure is approved

The disclosure approver sees only its packet: the latest version that has no approval, and the versions it approved before. It does not see the rest of the case. It checks the recipient, the purpose and the digest, and then approves the version.

- **Disclosure approver:** approves this version only. A later version needs its own approval.
- **Institution owner:** if the approver is not available, the backup approver takes over after the waiting time. The institution owner assigns the backup approvers and sets the waiting time. It does not approve.

### 8. The external notice is recorded and the case is closed

After the approval, the notice sent to the external party is recorded against the approved version. The triager closes the case only when the deadline tasks, the review layers and the notices are finished.

- **Disclosure approver:** records the notice of the version it approved.
- **Triager:** closes the case. The reason is recorded with the closure.

### Throughout the flow

- **Reporting owner:** reads the reporter contact when the organisation answers the reporter. Each read is audited.
- **Auditor:** reads the audit trail and the overview counts. It sees no case content.
- **Administrator:** keeps the accounts working. It does not see the case while the case is in progress.

## Why the roles are separate

- **The person who prepares a disclosure cannot approve it.** The approver is always someone else.
- **The approver sees only its packet.** It does not need the whole case to decide.
- **The administrator has no case rights.** Platform administration does not reach case content. This keeps the people who run the system apart from the people who decide about the incident.
- **The reporter sees only its own cases.** Reporter identities stay hidden from the team that works the case, and only the reporting owner can reach them.
- **The auditor reads and changes nothing.** It can check the record without being able to alter it.
- **The institution owner keeps the notice process running** when an approver is missing, without being able to approve.

A small team can give one person more than one role, for example a triager who also investigates. The application does not allow the combinations that would remove a check: a preparer who approves, or an administrator who reads case content.
