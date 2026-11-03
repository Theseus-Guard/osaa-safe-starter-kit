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
  // Disclosure versions waiting for approval: disclosure approver (disclosures:approve).
  approvals: boolean;
  // Open significant risks and the weekly updates of the caller's cases: investigator and reviewer.
  weeklyUpdates: boolean;
}

// The roles with the weekly update panel (backend overview.WEEKLY_UPDATE_ROLES).
const WEEKLY_UPDATE_ROLES = ["investigator", "reviewer"];

const OWN_CASE_PERMISSIONS = ["cases:read_own", "cases:read_assigned"] as const;

export function overviewSections(profile: Pick<Me, "role" | "permissions">): OverviewSections {
  const has = (name: string): boolean => profile.permissions.includes(name);
  return {
    ownCases: OWN_CASE_PERMISSIONS.some(has),
    operations: has("audit:read") || profile.role === "admin",
    users: has("users:manage"),
    logins: has("users:manage"),
    approvals: has("disclosures:approve"),
    weeklyUpdates: WEEKLY_UPDATE_ROLES.includes(profile.role),
  };
}
