# User guide

SAFE-Starter-Kit is a self-hosted web application for the SAFE workflow for AI incidents and near misses. A team uses it to record an incident as a case, follow the SAFE deadlines that are calculated from the awareness time, keep evidence with a custody log, record reviews, prepare disclosure package versions for human approval, and record the external notices that a person sent. It also keeps a versioned catalog of defences with their verification history.

This is early development software. It is not a production system, it has no single sign-on, and it must not be used with real incident data yet. The project is developed to conform to SAFE v1.0. It is not certified or endorsed by the Open Secure AI Alliance. The [Overview](../../README.md) has the full status.

## Who uses it

Each account has one role. The [Roles and permissions](./roles-and-permissions.md) page lists what each role can see and do, the fixed rules, and the suggested holder of each role.

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
