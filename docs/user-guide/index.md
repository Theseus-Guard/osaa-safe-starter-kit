# User guide

SAFE-Starter-Kit is a self-hosted web application for the SAFE workflow for AI incidents and near misses. A team uses it to record an incident as a case, follow the SAFE deadlines that are calculated from the awareness time, keep evidence with a custody log, record reviews, prepare disclosure package versions for human approval, and record the external notices that a person sent. It also keeps a versioned catalog of defences with their verification history.

This is early development software. It is not a production system, it has no single sign-on, and it must not be used with real incident data yet. The project is developed to conform to SAFE v1.0. It is not certified or endorsed by the Open Secure AI Alliance. The [Overview](../../README.md) has the full status.

## Who uses it

Each account has one role. The table shows what each role can see and do with the permissions that a new installation grants. An administrator can change these permissions on the **Roles and permissions** page, except for the fixed rows described below.

| Role | Used for | Can see | Main actions |
|---|---|---|---|
| `reporter` | Reporting an incident | Only the cases the reporter opened | Open a case |
| `triager` | Triage of cases | Every case, except reporter-channel cases that have no assignment yet | Record impacts, classification, awareness events, legal duties and links; assign investigators and reviewers; close and reopen cases; mark deadlines not applicable; prepare disclosure versions |
| `investigator` | Work on assigned cases | Cases it is assigned to, their evidence and the catalog | Upload evidence; change the status of deadline tasks |
| `reviewer` | Review of assigned cases | Cases it is assigned to, their evidence and the catalog | Open review rounds and set review layers; declare conflicts; file appeals; write catalog entries |
| `disclosure_approver` | Approval of disclosure versions | Every case, except reporter-channel cases that have no assignment yet, and the disclosure versions | Approve a version; record notices of an approved version; export an approved version |
| `auditor` | Oversight of the installation | Overview counts and the catalog | Read only |
| `reporting_owner` | Holding the reporter contact | Every case, and the reporter contact | Read the reporter contact; every read is audited |
| `admin` | Running the installation | Users, roles, the privacy inventory and the system timezone. Not case content | Manage accounts, roles and permissions, the timezone and the privacy export |

Two rows are fixed. The `admin` role always keeps the permissions to manage users, reset passwords and manage roles, so administrators cannot lock themselves out. The `reporting_owner` role cannot be changed on the Roles page. No role can be given the reporter contact permission except this one.

Times are shown in the system timezone that an administrator chose. Stored times are always UTC.

## Map of the screens

| Screen | Address in the browser | Shown to |
|---|---|---|
| Overview | `#/overview` | Every signed-in user. The cards depend on the role |
| Cases | `#/cases`, and `#/cases/{id}` for one case | Roles with a case read permission |
| Catalog | `#/catalog`, and `#/catalog/{id}` for one entry | Roles with `catalog:read` |
| Users | `#/users` | Administrators |
| Data privacy | `#/privacy` | Administrators |
| Roles and permissions | `#/roles` | Administrators |
| Settings | `#/settings` | Administrators |
| Account | `#/account` | Every signed-in user |

A user who opens an administrator page without the admin role is sent to the Overview. The Cases and Catalog pages stay open and show a message that the role has no access.

## Where to go next

- [Quick start](./quick-start.md): from a clean machine to the first sign-in.
- [Screens](./screens.md): each screen, its tabs, who can change what, and what is audited.
- [Workflows](./workflows.md): the steps of the main workflows, and what is not built yet.
- [Demo](./demo.md): the synthetic demo and its sign-in roles.
- [FAQ](./faq.md): short answers to common questions.
