import { describe, expect, it } from "vitest";
import { canReadCases, caseAbilities, hasPermission } from "./permissions";

describe("permissions", () => {
  it("lets any case-read permission open the case search", () => {
    expect(canReadCases(["cases:read_own"])).toBe(true);
    expect(canReadCases(["cases:read_all"])).toBe(true);
    expect(canReadCases(["cases:read_assigned"])).toBe(true);
  });

  it("keeps the case search from callers without a case-read permission", () => {
    expect(canReadCases([])).toBe(false);
    expect(canReadCases(["cases:submit", "cases:close", "users:manage"])).toBe(false);
  });

  it("matches one exact permission name", () => {
    expect(hasPermission(["cases:close"], "cases:close")).toBe(true);
    expect(hasPermission(["cases:close"], "cases:close_all")).toBe(false);
  });

  it("maps the case record permissions: read with any case-read permission, write with triage, full view with read_all", () => {
    const reporter = caseAbilities(["cases:read_own"]);
    expect(reporter.canReadRecords).toBe(true);
    expect(reporter.canWriteRecords).toBe(false);
    expect(reporter.canReadFullView).toBe(false);

    const triager = caseAbilities(["cases:read_all", "cases:triage"]);
    expect(triager.canReadRecords).toBe(true);
    expect(triager.canWriteRecords).toBe(true);
    expect(triager.canReadFullView).toBe(true);

    const reviewer = caseAbilities(["cases:read_assigned", "reviews:write"]);
    expect(reviewer.canReadRecords).toBe(true);
    expect(reviewer.canWriteRecords).toBe(false);
  });

  it("grants no case ability without permissions", () => {
    expect(Object.values(caseAbilities([])).every((allowed) => allowed === false)).toBe(true);
  });

  it("maps the triager permissions to the case controls", () => {
    expect(caseAbilities(["cases:read_all", "cases:triage", "cases:assign", "cases:close"])).toEqual({
      canSubmit: false,
      canClose: true,
      canUpdateDeadlines: true,
      canMarkNotApplicable: true,
      canAssign: true,
      canReadEvidence: false,
      canWriteEvidence: false,
      canReview: false,
      canReadRecords: true,
      canWriteRecords: true,
      canReadFullView: true,
      canReadReporterContact: false,
    });
  });

  it("lets only reporter_contact:read open the reporter contact", () => {
    expect(caseAbilities(["reporter_contact:read"]).canReadReporterContact).toBe(true);
    expect(caseAbilities(["cases:read_all", "cases:assign"]).canReadReporterContact).toBe(false);
  });

  it("lets a reporter submit cases but not change them", () => {
    const abilities = caseAbilities(["cases:submit", "cases:read_own"]);

    expect(abilities.canSubmit).toBe(true);
    expect(abilities.canClose).toBe(false);
    expect(abilities.canUpdateDeadlines).toBe(false);
  });

  it("lets an assigned investigator update deadlines without closing or marking not applicable", () => {
    const abilities = caseAbilities(["cases:read_assigned", "evidence:read", "evidence:write"]);

    expect(abilities.canUpdateDeadlines).toBe(true);
    expect(abilities.canClose).toBe(false);
    expect(abilities.canMarkNotApplicable).toBe(false);
  });

  it("lets only a caller with reviews:write record a review", () => {
    expect(caseAbilities(["reviews:write"])).toMatchObject({ canReview: true });
    expect(caseAbilities(["cases:read_assigned", "evidence:read"])).toMatchObject({ canReview: false });
  });

  it("keeps evidence read and evidence write apart", () => {
    expect(caseAbilities(["evidence:read"])).toMatchObject({ canReadEvidence: true, canWriteEvidence: false });
    expect(caseAbilities(["evidence:write"])).toMatchObject({ canReadEvidence: false, canWriteEvidence: true });
  });
});
