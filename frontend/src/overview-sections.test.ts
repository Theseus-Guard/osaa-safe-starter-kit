import { describe, expect, it } from "vitest";
import { overviewSections } from "./overview-sections";

// The permission names each role holds by default (backend/src/safe_kit/rbac.py).
const DEFAULT_PERMISSIONS: Record<string, string[]> = {
  reporter: ["cases:submit", "cases:read_own"],
  triager: ["cases:read_assigned", "cases:read_all", "cases:triage", "cases:assign", "cases:close", "evidence:read"],
  investigator: ["cases:read_assigned", "cases:assignable", "evidence:read", "evidence:write"],
  reviewer: ["cases:read_assigned", "cases:assignable", "evidence:read", "reviews:write"],
  disclosure_approver: ["disclosures:approve"],
  auditor: ["audit:read"],
  reporting_owner: ["cases:read_all", "reporter_contact:read"],
  admin: ["users:manage", "users:reset_password", "roles:manage", "privacy:read"],
};

function sectionsFor(role: string) {
  return overviewSections({ role: role as never, permissions: DEFAULT_PERMISSIONS[role] });
}

describe("overviewSections", () => {
  it.each(["reporter", "triager", "investigator", "reviewer"])("gives %s its own case counts only", (role) => {
    expect(sectionsFor(role)).toMatchObject({ ownCases: true, operations: false, users: false, logins: false });
  });

  it.each(["investigator", "reviewer"])("gives %s the weekly update panel", (role) => {
    expect(sectionsFor(role)).toMatchObject({ weeklyUpdates: true, approvals: false });
  });

  it.each(["reporter", "triager"])("gives %s no weekly update panel", (role) => {
    expect(sectionsFor(role)).toMatchObject({ weeklyUpdates: false });
  });

  it("gives auditor the operations sections and no user or login data", () => {
    expect(sectionsFor("auditor")).toEqual({
      ownCases: false,
      operations: true,
      users: false,
      logins: false,
      approvals: false,
      weeklyUpdates: false,
    });
  });

  it("gives admin every section except the approval and weekly panels", () => {
    expect(sectionsFor("admin")).toEqual({
      ownCases: false,
      operations: true,
      users: true,
      logins: true,
      approvals: false,
      weeklyUpdates: false,
    });
  });

  it("gives the disclosure approver the approval panel only", () => {
    expect(sectionsFor("disclosure_approver")).toEqual({
      ownCases: false,
      operations: false,
      users: false,
      logins: false,
      approvals: true,
      weeklyUpdates: false,
    });
  });

  it("gives reporting owner no overview section", () => {
    expect(sectionsFor("reporting_owner")).toEqual({
      ownCases: false,
      operations: false,
      users: false,
      logins: false,
      approvals: false,
      weeklyUpdates: false,
    });
  });

  it("follows the permission names, not the role name, for own cases", () => {
    const sections = overviewSections({ role: "auditor", permissions: ["cases:read_own"] });

    expect(sections.ownCases).toBe(true);
  });

  it("follows the approve permission, not the role name, for approvals", () => {
    const sections = overviewSections({ role: "auditor", permissions: ["disclosures:approve"] });

    expect(sections.approvals).toBe(true);
  });
});
