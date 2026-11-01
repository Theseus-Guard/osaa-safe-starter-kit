// Maps the caller's permission names (from /auth/me) to the controls the case screens show.
// The backend enforces every permission. These checks only hide or lock controls the caller cannot use.

export interface CaseAbilities {
  // cases:submit. Create a case.
  canSubmit: boolean;
  // cases:close. Close and reopen a case.
  canClose: boolean;
  // cases:read_all or cases:read_assigned. Change deadline status.
  canUpdateDeadlines: boolean;
  // cases:triage. Mark a deadline not applicable.
  canMarkNotApplicable: boolean;
  // cases:assign. Remove an assignment.
  canAssign: boolean;
  // evidence:read. List evidence, download it and read its custody log.
  canReadEvidence: boolean;
  // evidence:write. Upload evidence.
  canWriteEvidence: boolean;
  // reviews:write. Record a review. The backend also requires an assignment to the case.
  canReview: boolean;
}

export function hasPermission(permissions: readonly string[], name: string): boolean {
  return permissions.includes(name);
}

export function caseAbilities(permissions: readonly string[]): CaseAbilities {
  return {
    canSubmit: hasPermission(permissions, "cases:submit"),
    canClose: hasPermission(permissions, "cases:close"),
    canUpdateDeadlines:
      hasPermission(permissions, "cases:read_all") || hasPermission(permissions, "cases:read_assigned"),
    canMarkNotApplicable: hasPermission(permissions, "cases:triage"),
    canAssign: hasPermission(permissions, "cases:assign"),
    canReadEvidence: hasPermission(permissions, "evidence:read"),
    canWriteEvidence: hasPermission(permissions, "evidence:write"),
    canReview: hasPermission(permissions, "reviews:write"),
  };
}
