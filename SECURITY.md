# Security Policy

## Status

SAFE-Starter-Kit is in early development. It is not a production system: it has HTTPS only with a development certificate, no OIDC/SSO, and no encryption at rest for the database yet. Evidence files are encrypted by the application, with the key read from a file. Do not deploy it with real incident data.

## Supported versions

Only the latest commit on the `main` branch and the active feature branch receive security fixes. There are no released versions yet.

## Reporting a vulnerability

Do not open a public issue for a security problem.

- Preferred: use GitHub private vulnerability reporting in this repository (Security tab, "Report a vulnerability"), once it is enabled for the repository.
- Alternative: email the maintainer privately at hakan.damar@linux.com with the subject line "SAFE-Starter-Kit security report".

Please include:

- the affected component (for example `backend/src/safe_kit/api.py`, `gateway/nginx.conf`, `db/init/10-roles.sh`);
- the commit hash you tested;
- steps to reproduce, and the expected and actual behaviour;
- the impact you believe it has.

We aim to acknowledge a report within 5 working days and to tell you what we plan to do. We will credit reporters who want to be credited.

## Scope

In scope: the code in this repository, its Docker configuration, and its database role setup.

Out of scope:

- vulnerabilities in the SAFE specification itself. Report those to the SAFE Working Group in the [wg-safe repository](https://github.com/OpenSecureAIAlliance/wg-safe), as described in the README;
- vulnerabilities in third-party images or libraries. Report them to their maintainers, and tell us if our pinned version is affected;
- findings that depend on a deployment we do not recommend, such as exposing the development gateway on a network without TLS.

## Disclosure

We ask for coordinated disclosure: please give us a reasonable time to fix a problem before publishing details.
