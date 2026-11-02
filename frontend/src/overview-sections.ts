// Which overview sections the caller sees. These checks mirror the gates in backend/src/safe_kit/overview.py and the
// role matrix in plans/f1/overview-dashboard.md. The backend enforces every rule. The UI uses these checks only to
// decide what to render, so a section the caller may not see sends no request at all.
import type { Me } from "./api";

export interface OverviewSections {
  // Own case and deadline counts: reporter, triager, investigator and reviewer.
  ownCases: boolean;
  // Anonymous case counts, audit actions, outbox and worker health: auditor and admin.
  operations: boolean;
  // User distribution and pending password changes: admin.
  users: boolean;
  // Login successes and failures: admin.
  logins: boolean;
}

const OWN_CASE_PERMISSIONS = ["cases:read_own", "cases:read_assigned"] as const;

export function overviewSections(profile: Pick<Me, "role" | "permissions">): OverviewSections {
  const has = (name: string): boolean => profile.permissions.includes(name);
  return {
    ownCases: OWN_CASE_PERMISSIONS.some(has),
    operations: has("audit:read") || profile.role === "admin",
    users: has("users:manage"),
    logins: has("users:manage"),
  };
}
