# Contributing

Thank you for helping improve SAFE-Starter-Kit. Please read this page before you open an issue or a pull request.

## Before you start

- For a security problem, follow [SECURITY.md](SECURITY.md). Do not open a public issue.
- For a bug or a feature, open an issue first so the change can be discussed. Use the issue templates.
- Check the project scope in [ROADMAP.md](ROADMAP.md) and the Unreleased section of [CHANGELOG.md](CHANGELOG.md), which lists what is built and what is not yet. Features outside the current phase may be deferred.

## Development setup

- Backend: Python 3.13 with [uv](https://docs.astral.sh/uv/). Run the tests with `scripts/test-backend.sh`. Use `scripts/test-backend.sh --db` for the database-backed tests; it starts a throwaway PostgreSQL container.
- Frontend: Node.js with npm. In `frontend/`, run `npm ci`, then `npx vitest run` and `npm run build`.
- Local stack: see the Run section of the README.

## Pull requests

- Work on a feature branch. Do not commit directly to `main`.
- Keep a pull request to one topic. Describe what changed and why, and list the tests you ran.
- Add or update tests for every behaviour change. Backend and frontend tests must pass before review.
- Database changes are forward-only migrations in `backend/migrations`. Never edit an applied migration.
- Application code never deletes rows. Use the soft-delete columns described in the README and in `backend/tests/test_soft_delete_rule.py`.
- Code, comments, UI strings, log and CLI messages, and public documents are written in English.
- Do not commit credentials, certificates, or local secrets. The `secrets/` directory is git-ignored and must stay that way.

## User guide

The user guide in `docs/user-guide/` is published on the documentation site. After any phase, and after any change that adds or changes a screen, a role, a workflow or a command, update the guide pages that cover it in the same change. Check these files:

- [ ] `docs/user-guide/index.md`: the roles and their permissions, and the map of the screens.
- [ ] `docs/user-guide/quick-start.md`: the setup and sign-in commands.
- [ ] `docs/user-guide/screens.md`: each screen and tab, the labels, who can change what, and what is audited.
- [ ] `docs/user-guide/workflows.md`: the workflows, and the list of features that are not built yet.
- [ ] `docs/user-guide/demo.md`: the demo commands, the demo roles, and what was run live.
- [ ] `docs/user-guide/faq.md`: every answer that is no longer true.

## Licence

By contributing, you agree that your contribution is licensed under the Apache License 2.0, the licence of this project.

## Code of conduct

Participation is governed by the [Code of Conduct](CODE_OF_CONDUCT.md).
