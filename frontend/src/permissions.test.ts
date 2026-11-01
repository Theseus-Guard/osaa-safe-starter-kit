import { describe, expect, it } from "vitest";
import { caseAbilities, hasPermission } from "./permissions";

describe("permissions", () => {
  it("matches one exact permission name", () => {
    expect(hasPermission(["cases:close"], "cases:close")).toBe(true);
    expect(hasPermission(["cases:close"], "cases:close_all")).toBe(false);
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
    });
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

  it("keeps evidence read and evidence write apart", () => {
    expect(caseAbilities(["evidence:read"])).toMatchObject({ canReadEvidence: true, canWriteEvidence: false });
    expect(caseAbilities(["evidence:write"])).toMatchObject({ canReadEvidence: false, canWriteEvidence: true });
  });
});
