# First day as an administrator

This page is a checklist for the first day with a new installation. It assumes the stack is running and you have the first administrator's password. Each step links to the page with the details.

Read [Concepts](./concepts.md) first if the terms are new.

## 1. Sign in and set the timezone

1. Sign in at https://127.0.0.1:8443 and choose a new password when asked.
2. Open **Settings** and choose the system timezone. Every time on every screen is shown in that timezone; stored times stay in UTC.

## 2. Decide who holds which role

Before you create accounts, decide who does what. [Roles and permissions](./roles-and-permissions.md) lists each role, what it can see, and a suggested holder. At least these roles are needed for a working installation:

- one or more **triagers**, to open, assign, and close cases;
- one or more **investigators** and **reviewers**, to work on the cases;
- at least two **disclosure approvers**, so that one can approve when the other is away;
- **institution owners**, two to four people, if you use external notices with backup approvers;
- an **auditor**, if someone must oversee the installation without case access;
- a **reporting owner**, if reporter contacts are used.

Keep the number of administrators small. An administrator manages accounts but cannot see case content.

## 3. Create the accounts

1. Open **Users** and create one account per person. Use a personal account for each person; do not share accounts.
2. Give each account one role. Each new account must change its password at its first sign-in.
3. Do not try to give the `institution_owner` role on this page. The page refuses it. The operator gives it with the `owner grant` command (see [Operator guide](./operator-guide.md#operator-commands)).

## 4. Check the permissions

Open **Roles and permissions** and read the table for each role. You can change most permissions there. Do not change the fixed rows, and do not try to give a case permission to the administrator role; the application refuses it.

## 5. Try the workflow with a test case

Use an account that is not used for real incidents. Follow [Workflows](./workflows.md):

1. Open a case with an awareness time, and check the deadline tasks it creates.
2. Assign an investigator and a reviewer.
3. Record a review round.
4. Close the case, and reopen it.

## 6. Set up the backup approvers (API only)

The backup approvers of a case are not set on a screen yet. An institution owner sets them through the API, and an administrator cannot do this step:

- A case needs a pool of at least two distinct, active disclosure approvers, and the pool is replaced in one request: `PUT /api/v1/cases/{case_id}/notice-backups` with `{"backup_user_ids": [...]}`.
- The waiting time before a backup may act is 4 hours by default, between 1 and 168 hours. An institution owner changes it with `PUT /api/v1/notice-settings/backup-wait` and a reason.

See [Workflows, section 14](./workflows.md#14-backup-approval-api-only) for the escalation steps. Send the requests with the access token from `POST /api/v1/auth/login`, as a `Bearer` header.

## 7. Before you use it for a real incident

Read [Operator guide, What is not built](./operator-guide.md#what-is-not-built). The application is not yet a production system. It has a development certificate, no single sign-on, and no backup procedure of its own.
