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
  // evidence:read and evidence:write. Make a redacted copy of a text original. The backend also checks the case and the TLP rule.
  canRedactEvidence: boolean;
  // reviews:write. Record a review. The backend also requires an assignment to the case.
  canReview: boolean;
  // Any case-read permission. Shows the case record sections: awareness events, impacts, legal duties and classification.
  canReadRecords: boolean;
  // cases:triage. Write the case record: awareness events, impacts, legal duties and the classification.
  canWriteRecords: boolean;
  // cases:read_all. The full view: free text of the case record, and the preliminary report.
  canReadFullView: boolean;
  // reporter_contact:read. Read the contact the reporter gave. Every read is audited.
  canReadReporterContact: boolean;
}

export function hasPermission(permissions: readonly string[], name: string): boolean {
  return permissions.includes(name);
}

const CASE_READ_PERMISSIONS = ["cases:read_own", "cases:read_all", "cases:read_assigned"] as const;

// Any case-read permission. Shows the header case search. The backend still filters the results to the cases the caller can see.
export function canReadCases(permissions: readonly string[]): boolean {
  return CASE_READ_PERMISSIONS.some((name) => hasPermission(permissions, name));
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
    canRedactEvidence:
      hasPermission(permissions, "evidence:read") && hasPermission(permissions, "evidence:write"),
    canReview: hasPermission(permissions, "reviews:write"),
    canReadRecords: canReadCases(permissions),
    canWriteRecords: hasPermission(permissions, "cases:triage"),
    canReadFullView: hasPermission(permissions, "cases:read_all"),
    canReadReporterContact: hasPermission(permissions, "reporter_contact:read"),
  };
}

export interface CatalogAbilities {
  // catalog:read. Open the catalog list, the entries, their versions and the recommendations.
  canRead: boolean;
  // catalog:write. Record an entry, a version, a correction, a dispute, a withdrawal, a supersession, a recommendation or a verification run.
  canWrite: boolean;
  // catalog:read_restricted. See a restricted reference and its digest. The API omits them otherwise.
  canReadRestricted: boolean;
}

export function catalogAbilities(permissions: readonly string[]): CatalogAbilities {
  return {
    canRead: hasPermission(permissions, "catalog:read"),
    canWrite: hasPermission(permissions, "catalog:write"),
    canReadRestricted: hasPermission(permissions, "catalog:read_restricted"),
  };
}
