# Roles and permissions

Each account has one role. A role holds a set of permissions, and the permissions decide what the account can see and do. This page lists the permissions of each role in a new installation, after all database migrations up to `0057`.

An administrator can change most of these permissions on the **Roles and permissions** page. The fixed rows are described below the table.

## What each role can see and do

| Role | Permissions | Can see | Can do | Cannot do |
|---|---|---|---|---|
| `reporter` | `cases:submit`, `cases:read_own` | Only the cases it opened | Open a case | Read any other case |
| `triager` | `cases:read_all`, `cases:read_assigned`, `cases:triage`, `cases:assign`, `cases:close`, `disclosures:prepare`, `disclosures:read`, `evidence:read`, `catalog:read` | Every case, including reporter-channel cases before assignment. The reporter identity and contact stay hidden | Record impacts, classification, awareness events, legal duties and links; assign investigators and reviewers; close and reopen cases; mark deadlines not applicable; prepare disclosure versions | Approve a disclosure version; read the reporter contact; manage users |
| `investigator` | `cases:read_assigned`, `cases:assignable`, `evidence:read`, `evidence:write`, `catalog:read` | Cases it is assigned to, their evidence, and the catalog | Upload evidence; change the status of deadline tasks | Assign or close cases; approve disclosures |
| `reviewer` | `cases:read_assigned`, `cases:assignable`, `evidence:read`, `reviews:write`, `catalog:read`, `catalog:write` | Cases it is assigned to, their evidence, and the catalog | Open review rounds and set review layers; write catalog entries | Assign or close cases; approve disclosures |
| `disclosure_approver` | `disclosures:approve`, `disclosures:read` | Only the disclosure version it is assigned to approve (its packet), and the missing facts of that packet. It does not see the case | Approve the packet; record notices of an approved version; export an approved version | Read the case; change deadlines; file appeals |
| `auditor` | `audit:read`, `catalog:read` | Overview counts and the catalog | Read only | Read any case content |
| `reporting_owner` | `cases:read_all`, `reporter_contact:read` | Every case, and the reporter contact | Read the reporter contact; every read is audited | Change any case |
| `institution_owner` | `cases:read_all`, `disclosures:read`, `disclosures:assign_backup`, `disclosures:set_backup_wait` | Every case, and the disclosure records | Assign the backup approvers of a case; change the backup waiting time, with a reason | Approve, prepare or triage; read the reporter contact; manage users or roles |
| `admin` | `users:manage`, `users:reset_password`, `roles:manage`, `privacy:read` | Users, roles, the privacy inventory, and the system timezone | Manage accounts, roles and permissions; reset passwords; run the privacy export | Read or change any case content; change or reset an `institution_owner` account |

## Fixed rules

These rules are enforced by the application. An administrator cannot change them on the Roles page.

- The `admin` role always keeps `users:manage`, `users:reset_password` and `roles:manage`, so administrators cannot lock themselves out.
- The `admin` role never holds a case permission (`cases:`, `evidence:`, `reviews:`, `disclosures:`, `reporter_contact:`).
- The `reporting_owner` role cannot be changed. No other role can hold `reporter_contact:read`.
- The `institution_owner` role keeps `disclosures:assign_backup` and `disclosures:set_backup_wait`. No other role can hold these two permissions.
- No account can be created, given, removed, or changed through the application with the `institution_owner` role. An administrator cannot reset the password of an `institution_owner` account. The password is reset by the operator in the container, with `python -m safe_kit.admin reset-password`.
- The preparer of a disclosure version cannot approve it.
- A backup approver cannot be the primary approver of the same version, and nobody can name themselves as a backup.

## Suggested assignment in an organisation

The table below is a recommendation. The organisation decides who holds each role.

| Role | Suggested holder | How many | Reason |
|---|---|---|---|
| `reporter` | Anyone who reports an incident, inside or outside the organisation | Any | Sees only its own cases |
| `investigator` | The analyst who works the incident | One per team | Works only on assigned cases |
| `reviewer` | A person who reviews the work, independent of the investigator | One per team | Reviews assigned cases |
| `triager` | The incident coordinator who assigns and closes cases | Few | Sees every case and assigns the work |
| `disclosure_approver` | A manager with the authority to release information outside the organisation | At least two, so one can approve when the other is away | Approves the packets. Keep the approver separate from the preparer |
| `auditor` | Internal audit or compliance | Few | Read-only oversight |
| `reporting_owner` | The officer who holds reporter identities, for example data protection or legal | A small group | Reads the reporter contact; every read is audited |
| `institution_owner` | The senior operations manager who runs the external notice process | Two to four, with at least two available at any time | Sets the backup approvers of a case and the waiting time. It does not approve, so it needs time to set the backup pool before an urgent notice |
| `admin` | The platform or IT administrator | Few | Manages accounts and roles. Has no case content |

## How roles are assigned

- An administrator creates accounts on the **Users** page and gives each account one role. The administrator cannot give the `institution_owner` role.
- The `institution_owner` role is granted and revoked by the operator in the container, not in the application. Run these commands inside the API container:
  - Grant: `python -m safe_kit.admin owner grant --username <name> --reason "<why>"`
  - Revoke: `python -m safe_kit.admin owner revoke --username <name> --role <role> --reason "<why>"`
- Each command needs a reason of at most 500 characters. It writes an audit row with the roles before and after, and ends the account's sessions. The account must be active and must not be the first administrator.
- To disable or delete an account that holds `institution_owner`, revoke the role first. The application refuses both actions for that role.
- Each holder of a role uses a personal account. Do not share an account: the application records who made each change, and a shared account hides it.
