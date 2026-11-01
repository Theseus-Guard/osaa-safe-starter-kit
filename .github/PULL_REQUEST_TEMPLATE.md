## Summary

<!-- What this pull request changes and why. Link the issue it closes. -->

## Tests

- [ ] `scripts/test-backend.sh` passes (and `--db` if database code changed)
- [ ] `npx vitest run` and `npm run build` pass in `frontend/` (if frontend code changed)
- [ ] New or changed behaviour has tests

## Checklist

- [ ] Work is on a feature branch, not `main`
- [ ] Database changes are new forward-only migrations
- [ ] No rows are deleted in application code; soft-delete columns are used
- [ ] Code, comments, UI strings, and public documents are in English
- [ ] No credentials, certificates, or secrets are included
- [ ] Documentation (README, CHANGELOG) is updated when behaviour changes
