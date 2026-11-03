# SAFE-Starter-Kit Roadmap

SAFE-Starter-Kit is an independent, open-source starter kit that helps organizations taking part in the Shared AI Findings Exchange (SAFE) run their AI incident workflow: intake, triage, evidence preservation, review, controlled disclosure, and tracked corrective actions. It is a community project. It is not an official SAFE, OSAA, or committee product. The phases below describe intended goals. No dates are committed, and a phase is not complete until it has passed independent review.

## F0 — Foundations and Decisions

This phase settles the questions that every later phase depends on: project naming and licensing, a threat model, the data and API contracts, roles and responsibilities for a single-organization deployment, retention and deadline rules, and the target deployment environment. Its output is a recorded set of decisions that the rest of the work can rely on.

## F1 — Core Skeleton

Status: complete. Merged to `main` (PR #1).

This phase builds the minimal working system: a containerized stack with PostgreSQL, a one-time migration step, an API, a background worker, a web gateway, and a command-line client that all use the same API. It then adds the core domain model for cases, deadlines, and evidence, with access control enforced in one place, an audit trail, and a reliable outbox for side effects. The goal is a service that starts with Docker Compose and whose core flows work end to end.

## F2 — Domain Completion and Demo

This phase completes the review and disclosure workflow: the eleven-layer review framework, conflict and recusal handling, recommendations and a versioned catalog, redaction and approval before any export, and manual recording of external notices. It also adds a synthetic demo dataset and a guided demo that uses both the web interface and the command-line client, with external network access and notifications disabled.

Status: engineering and tests complete on the integration branch (not yet merged to `main`). Not done: the independent and human review gates, the live three-step demo run (only parts were run on the local stack), and the items deferred to F3 (escalation and alert records, recipient and package review records, backup approver, cursor paging for the other list endpoints, the 30-day analysis task, and the official holiday calendar).

## F3 — Hardening and Release

This phase prepares the project for real use. It covers synthetic exercises and a limited, approved pilot, backup and restore procedures with measured recovery objectives, migration and rollback rehearsals, accessibility work, pinned multi-architecture images with a software bill of materials and provenance, and security and license reviews. A release is made only after these gates pass and the maintainers approve it.

Status: next. It also takes the F2 items deferred above.

## F4 — Evidence-Driven Expansion

This phase extends the system only where measurements or real needs justify it. Candidates include object storage for evidence, caching if a measured bottleneck exists, a time-series database extension for high-volume metadata after benchmarking and license review, multi-tenant design if it becomes necessary, and formal interoperability with SAFE if the SAFE working group agrees on a specification.
