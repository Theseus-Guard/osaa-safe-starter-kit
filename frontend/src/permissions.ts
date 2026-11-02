// Maps the caller's permission names (from /auth/me) to the controls the case screens show.
// The backend enforces every permission. These checks only hide or lock controls the caller cannot use.

export interface CaseAbilities {
  // cases:submit. Create a case.
  canSubmit: boolean;
  // cases:close. Close and reopen a case.
  canClose: boolean;
  // cases:read_all or cases:read_assigned. Change deadline status. Not for the disclosure approver, which reads the case
  // through cases:read_all and is refused the deadline routes.
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
  // Any case-read permission, except for the disclosure approver. Shows the appeal form. The backend refuses appeals to
  // that role even though it reads the case.
  canFileAppeal: boolean;
  // Any case-read permission. Shows the case record sections: awareness events, impacts, legal duties and classification.
  canReadRecords: boolean;
  // cases:triage. Write the case record: awareness events, impacts, legal duties and the classification.
  canWriteRecords: boolean;
  // cases:read_all. The full view: free text of the case record, and the preliminary report.
  canReadFullView: boolean;
  // reporter_contact:read. Read the contact the reporter gave. Every read is audited.
  canReadReporterContact: boolean;
  // disclosures:read. List the disclosure package versions and their previews.
  canReadDisclosures: boolean;
  // disclosures:approve. Approve one disclosure version. The backend also requires case visibility.
  canApproveDisclosures: boolean;
  // disclosures:approve or disclosures:prepare. Record that an approved version was sent. Without disclosures:approve the
  // record is made on the manual path, with a reason. The backend also requires case visibility.
  canRecordNotices: boolean;
}

export function hasPermission(permissions: readonly string[], name: string): boolean {
  return permissions.includes(name);
}

const CASE_READ_PERMISSIONS = ["cases:read_own", "cases:read_all", "cases:read_assigned"] as const;

// Any case-read permission. Shows the header case search. The backend still filters the results to the cases the caller can see.
export function canReadCases(permissions: readonly string[]): boolean {
  return CASE_READ_PERMISSIONS.some((name) => hasPermission(permissions, name));
}

// The disclosure approver reads the case it approves through cases:read_all, but the deadline routes and the appeal route
// refuse that role by name (backend: require_any_permission_except_role). The role is needed to hide those controls.
export const DISCLOSURE_APPROVER_ROLE = "disclosure_approver";

export function caseAbilities(permissions: readonly string[], role?: string): CaseAbilities {
  const isApprover = role === DISCLOSURE_APPROVER_ROLE;
  const canReadRecords = canReadCases(permissions);
  return {
    canSubmit: hasPermission(permissions, "cases:submit"),
    canClose: hasPermission(permissions, "cases:close"),
    canUpdateDeadlines:
      (hasPermission(permissions, "cases:read_all") || hasPermission(permissions, "cases:read_assigned")) &&
      !isApprover,
    canMarkNotApplicable: hasPermission(permissions, "cases:triage"),
    canAssign: hasPermission(permissions, "cases:assign"),
    canReadEvidence: hasPermission(permissions, "evidence:read"),
    canWriteEvidence: hasPermission(permissions, "evidence:write"),
    canRedactEvidence:
      hasPermission(permissions, "evidence:read") && hasPermission(permissions, "evidence:write"),
    canReview: hasPermission(permissions, "reviews:write"),
    canFileAppeal: canReadRecords && !isApprover,
    canReadRecords,
    canWriteRecords: hasPermission(permissions, "cases:triage"),
    canReadFullView: hasPermission(permissions, "cases:read_all"),
    canReadReporterContact: hasPermission(permissions, "reporter_contact:read"),
    canReadDisclosures: hasPermission(permissions, "disclosures:read"),
    canApproveDisclosures: hasPermission(permissions, "disclosures:approve"),
    canRecordNotices:
      hasPermission(permissions, "disclosures:approve") || hasPermission(permissions, "disclosures:prepare"),
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
