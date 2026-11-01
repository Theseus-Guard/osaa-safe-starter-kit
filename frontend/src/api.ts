// Thin client for the existing backend API, called through the gateway at /api/v1.
// Tokens live in memory only. A page reload requires logging in again.

import { recordActivity } from "./idle";

export const ROLES = [
  "reporter",
  "triager",
  "investigator",
  "reviewer",
  "disclosure_approver",
  "auditor",
  "reporting_owner",
  "admin",
] as const;

export type Role = (typeof ROLES)[number];

// Contact and display details of an account. Optional names and title may be null. Email is required on new accounts.
export interface UserProfile {
  first_name: string | null;
  last_name: string | null;
  title: string | null;
  email: string | null;
}

// Only the fields to change. An empty string clears an optional name or title. Email cannot be cleared.
export type ProfileUpdate = Partial<{ first_name: string; last_name: string; title: string; email: string }>;

export interface Me extends UserProfile {
  id: string;
  username: string;
  role: Role;
  password_change_required: boolean;
  // True only for an admin when no system timezone is set yet.
  timezone_setup_required: boolean;
  // The caller's permission names. The UI uses them only to hide or lock controls. The backend checks every request.
  permissions: string[];
}

export interface CreatedUser {
  id: string;
  username: string;
  role: Role;
  password_change_required: boolean;
}

export interface UserSummary extends UserProfile {
  id: string;
  username: string;
  role: Role;
  is_active: boolean;
  is_protected: boolean;
  password_change_required: boolean;
  created_at: string;
}

export interface UserPage {
  items: UserSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface UserUpdate {
  role?: Role;
  is_active?: boolean;
}

export interface RoleSummary {
  role: Role;
  permissions: string[];
}

export interface PermissionSummary {
  name: string;
  description: string;
}

interface TokenResponse {
  access_token: string;
  refresh_token: string;
  expires_in: number;
  password_change_required: boolean;
}

export type Readiness = "ready" | "setup_required" | "unavailable";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  // Set only on a classification_version_conflict (409): the version the server holds now.
  readonly currentVersion: number | null;

  constructor(status: number, code: string, currentVersion: number | null = null) {
    super(code);
    this.status = status;
    this.code = code;
    this.currentVersion = currentVersion;
  }
}

interface Session {
  access: string;
  refresh: string;
  // When the current token pair was issued. Both tokens start their lifetime at the same moment.
  issuedAt: number;
}

// The pair is renewed by the first request made this long after it was issued. The refresh token has the same
// lifetime as the access token, so renewing only after the access token expires would find the refresh token expired
// too. A session that is not used for the full refresh lifetime ends on its next request.
const RENEW_AFTER_MS = 5 * 60 * 1000;

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  json?: unknown;
  // Set to false for requests that must not carry or renew the session (login, readiness).
  auth?: boolean;
  // "blob" returns the raw body for file downloads. It goes through the same auth and refresh path.
  responseType?: "json" | "blob";
  // Multipart body for file uploads. The browser sets the Content-Type with the boundary.
  form?: FormData;
}

let session: Session | null = null;
let refreshInFlight: Promise<boolean> | null = null;
let sessionEndedListener: (() => void) | null = null;

export function onSessionEnded(listener: () => void): void {
  sessionEndedListener = listener;
}

export function clearSession(): void {
  session = null;
}

function endSession(): void {
  session = null;
  sessionEndedListener?.();
}

function storeTokens(body: TokenResponse): void {
  session = { access: body.access_token, refresh: body.refresh_token, issuedAt: Date.now() };
}

async function send(path: string, options: RequestOptions): Promise<Response> {
  const headers = new Headers();
  if (options.json !== undefined) headers.set("Content-Type", "application/json");
  if (options.auth !== false && session) headers.set("Authorization", `Bearer ${session.access}`);
  let body: BodyInit | undefined;
  if (options.form !== undefined) body = options.form;
  else if (options.json !== undefined) body = JSON.stringify(options.json);
  return fetch(`/api/v1${path}`, {
    method: options.method ?? "GET",
    headers,
    body,
  });
}

async function renew(): Promise<boolean> {
  if (!session) return false;
  const response = await fetch("/api/v1/auth/refresh", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ refresh_token: session.refresh }),
  });
  if (!response.ok) return false;
  storeTokens((await response.json()) as TokenResponse);
  return true;
}

// Concurrent 401s share one refresh. Two parallel refreshes with the same token would look like
// token reuse to the backend, which revokes the whole session family.
function refreshOnce(): Promise<boolean> {
  refreshInFlight ??= renew().finally(() => {
    refreshInFlight = null;
  });
  return refreshInFlight;
}

async function toApiError(response: Response): Promise<ApiError> {
  let code = `http_${response.status}`;
  let currentVersion: number | null = null;
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string") {
      code = body.detail;
    } else if (typeof body.detail === "object" && body.detail !== null && "status" in body.detail) {
      const detail = body.detail as { status: unknown; current_version?: unknown };
      code = String(detail.status);
      if (typeof detail.current_version === "number") currentVersion = detail.current_version;
    }
  } catch {
    // Empty or non-JSON error body: keep the HTTP status code.
  }
  return new ApiError(response.status, code, currentVersion);
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  recordActivity();
  if (session && options.auth !== false && Date.now() - session.issuedAt > RENEW_AFTER_MS) {
    if (!(await refreshOnce())) {
      endSession();
      throw new ApiError(401, "session_expired");
    }
  }
  let response = await send(path, options);
  if (response.status === 401 && session && options.auth !== false) {
    if (await refreshOnce()) {
      response = await send(path, options);
    } else {
      endSession();
      throw new ApiError(401, "session_expired");
    }
  }
  if (!response.ok) throw await toApiError(response);
  if (response.status === 204) return undefined as T;
  if (options.responseType === "blob") return (await response.blob()) as T;
  return (await response.json()) as T;
}

export async function login(username: string, password: string): Promise<void> {
  const body = await request<TokenResponse>("/auth/token", {
    method: "POST",
    json: { username, password },
    auth: false,
  });
  storeTokens(body);
}

export function me(): Promise<Me> {
  return request<Me>("/auth/me");
}

export async function logout(): Promise<void> {
  const refreshToken = session?.refresh;
  try {
    await request("/auth/logout", {
      method: "POST",
      json: refreshToken ? { refresh_token: refreshToken } : {},
    });
  } finally {
    clearSession();
  }
}

// The backend ends every session on a password change, so the caller must log in again afterwards.
export async function changePassword(currentPassword: string, newPassword: string): Promise<void> {
  await request("/auth/password", {
    method: "POST",
    json: { current_password: currentPassword, new_password: newPassword },
  });
  clearSession();
}

export interface NewUser {
  username: string;
  role: Role;
  temporary_password: string;
  // Required by the backend.
  email: string;
  first_name?: string;
  last_name?: string;
  title?: string;
}

export function createUser(body: NewUser): Promise<CreatedUser> {
  return request<CreatedUser>("/users", { method: "POST", json: body });
}

// The caller's own contact details.
export function getProfile(): Promise<UserProfile> {
  return request<UserProfile>("/auth/profile");
}

export function updateProfile(body: ProfileUpdate): Promise<void> {
  return request("/auth/profile", { method: "PATCH", json: body });
}

// Admin edit of another account's contact details. The backend refuses the caller's own id (use updateProfile).
export function updateUserProfile(userId: string, body: ProfileUpdate): Promise<void> {
  return request(`/users/${encodeURIComponent(userId)}/profile`, { method: "PATCH", json: body });
}

export function listUsers(limit: number, offset: number): Promise<UserPage> {
  return request<UserPage>(`/users?limit=${limit}&offset=${offset}`);
}

export function updateUser(userId: string, changes: UserUpdate): Promise<UserSummary> {
  return request<UserSummary>(`/users/${encodeURIComponent(userId)}`, {
    method: "PATCH",
    json: changes,
  });
}

// Soft delete on the backend: the user is deactivated and hidden from lists, not physically removed.
export function deleteUser(userId: string): Promise<void> {
  return request(`/users/${encodeURIComponent(userId)}`, { method: "DELETE" });
}

export function resetUserPassword(userId: string, temporaryPassword: string): Promise<void> {
  return request(`/users/${encodeURIComponent(userId)}/password`, {
    method: "POST",
    json: { temporary_password: temporaryPassword },
  });
}

export function listRoles(): Promise<RoleSummary[]> {
  return request<RoleSummary[]>("/rbac/roles");
}

export function listPermissions(): Promise<PermissionSummary[]> {
  return request<PermissionSummary[]>("/rbac/permissions");
}

// Replaces the full permission set of one role. Changes take effect on the next request.
export function updateRolePermissions(role: Role, permissions: string[]): Promise<RoleSummary> {
  return request<RoleSummary>(`/rbac/roles/${encodeURIComponent(role)}/permissions`, {
    method: "PUT",
    json: { permissions },
  });
}

// Cases. The caller sees only the cases they created. Times are UTC ISO strings.
export interface DeadlineTask {
  // Null only for a task that is not stored yet. Such a task cannot be updated.
  id: string | null;
  kind: string;
  due_at: string;
  status: string;
  not_applicable_reason: string | null;
}

export type DeadlineStatus = "open" | "done" | "not_applicable";

export interface CaseSummary {
  id: string;
  reference: string;
  title: string;
  awareness_time: string;
  status: string;
  created_at: string;
}

export interface CaseDetail {
  id: string;
  reference: string;
  title: string;
  awareness_time: string;
  status: string;
  calendar_version: string;
  created_at: string;
  closed_at: string | null;
  close_reason: string | null;
  // Reason of the latest reopen. Null or absent when the case has never been reopened.
  reopen_reason?: string | null;
  deadlines: DeadlineTask[];
}

export interface CasePage {
  items: CaseSummary[];
  total: number;
  limit: number;
  offset: number;
}

export interface CaseCreateBody {
  title: string;
  // Timezone-aware UTC ISO string. The backend rejects a time in the future.
  awareness_time: string;
}

export interface ReportCreateBody extends CaseCreateBody {
  // Optional follow-up contact, at most 200 characters. Null or blank means no contact.
  contact?: string | null;
}

// q matches the case reference (partial, case-insensitive) or the case UUID prefix. Omitted or blank q lists every visible case.
export function listCases(limit: number, offset: number, q?: string): Promise<CasePage> {
  const term = q?.trim() ?? "";
  const search = term === "" ? "" : `&q=${encodeURIComponent(term)}`;
  return request<CasePage>(`/cases?limit=${limit}&offset=${offset}${search}`);
}

export function getCase(caseId: string): Promise<CaseDetail> {
  return request<CaseDetail>(`/cases/${encodeURIComponent(caseId)}`);
}

export function createCase(body: CaseCreateBody): Promise<CaseDetail> {
  return request<CaseDetail>("/cases", { method: "POST", json: body });
}

// Reporter intake. Returns the same shape as createCase. The contact is stored for the independent reporting owner and never returned.
export function createReport(body: ReportCreateBody): Promise<CaseDetail> {
  return request<CaseDetail>("/reports", { method: "POST", json: body });
}

// The reason is stored on the case and shown after closure.
export function closeCase(caseId: string, reason: string): Promise<CaseDetail> {
  return request<CaseDetail>(`/cases/${encodeURIComponent(caseId)}/close`, { method: "POST", json: { reason } });
}

// Needs cases:close, the same permission as closing. The reason is required and stored with the case.
export function reopenCase(caseId: string, reason: string): Promise<CaseDetail> {
  return request<CaseDetail>(`/cases/${encodeURIComponent(caseId)}/reopen`, { method: "POST", json: { reason } });
}

// The reason is required for not_applicable and rejected for every other status.
export function updateDeadline(
  caseId: string,
  taskId: string,
  status: DeadlineStatus,
  reason?: string,
): Promise<CaseDetail> {
  return request<CaseDetail>(`/cases/${encodeURIComponent(caseId)}/deadlines/${encodeURIComponent(taskId)}`, {
    method: "PATCH",
    json: { status, reason },
  });
}

// Review outcomes accepted by the backend.
export type ReviewOutcome = "pass" | "fail" | "insufficient_evidence" | "not_applicable";

export interface ReviewCreateBody {
  outcome: ReviewOutcome;
  // Omitted or null when the reviewer adds no note. The backend trims it and stores blank text as null.
  note?: string | null;
}

export interface ReviewResponse {
  id: string;
  case_id: string;
  outcome: ReviewOutcome;
  note: string | null;
}

// Needs reviews:write and an active assignment to the case. The case must be open.
export function createReview(caseId: string, body: ReviewCreateBody): Promise<ReviewResponse> {
  return request<ReviewResponse>(`/cases/${encodeURIComponent(caseId)}/reviews`, { method: "POST", json: body });
}

// Every review of the case, newest first and not deleted. Needs case read access. Answers 404 when the caller cannot see the case.
export function listReviews(caseId: string): Promise<ReviewResponse[]> {
  return request<ReviewResponse[]>(`/cases/${encodeURIComponent(caseId)}/reviews`);
}

export interface Assignment {
  id: string;
  case_id: string;
  user_id: string;
  username: string;
  role: Role;
  assigned_by: string;
  assigned_at: string;
}

export function listAssignments(caseId: string): Promise<Assignment[]> {
  return request<Assignment[]>(`/cases/${encodeURIComponent(caseId)}/assignments`);
}

export function addAssignment(caseId: string, userId: string): Promise<Assignment> {
  return request<Assignment>(`/cases/${encodeURIComponent(caseId)}/assignments`, {
    method: "POST",
    json: { user_id: userId },
  });
}

// Needs cases:assign. The users the caller may pick for an assignment on this case: active users in an assignable role.
export interface AssignableUser {
  id: string;
  username: string;
  first_name: string | null;
  last_name: string | null;
  role: Role;
}

export function listAssignableUsers(caseId: string): Promise<AssignableUser[]> {
  return request<AssignableUser[]>(`/cases/${encodeURIComponent(caseId)}/assignable-users`);
}

export function removeAssignment(caseId: string, userId: string): Promise<void> {
  return request(`/cases/${encodeURIComponent(caseId)}/assignments/${encodeURIComponent(userId)}`, {
    method: "DELETE",
  });
}

// A link between two cases that concern the same incident. Linking changes nothing else on either case.
// linked_case_id and linked_case_title are null when the caller cannot see the other case.
export interface CaseLink {
  linked_case_id: string | null;
  linked_case_title: string | null;
  created_at: string;
}

// Needs case read access. Answers 404 when the caller cannot see the case.
export function listCaseLinks(caseId: string): Promise<CaseLink[]> {
  return request<CaseLink[]>(`/cases/${encodeURIComponent(caseId)}/links`);
}

// Needs cases:triage. Both cases must be visible to the caller and open, and not linked yet.
export function addCaseLink(caseId: string, linkedCaseId: string): Promise<CaseLink> {
  return request<CaseLink>(`/cases/${encodeURIComponent(caseId)}/links`, {
    method: "POST",
    json: { linked_case_id: linkedCaseId },
  });
}

// Needs cases:triage. Soft deletes the link; the row is kept in the database.
export function removeCaseLink(caseId: string, linkedCaseId: string): Promise<void> {
  return request(`/cases/${encodeURIComponent(caseId)}/links/${encodeURIComponent(linkedCaseId)}`, {
    method: "DELETE",
  });
}

// Needs reporter_contact:read. Every call is written to the audit log, so call it only on an explicit user action.
// Resolves to null when no contact is recorded: the reporter gave none, or the case was not opened through the reporter channel.
export async function getReporterContact(caseId: string): Promise<string | null> {
  try {
    const body = await request<{ case_id: string; contact: string | null }>(
      `/reports/${encodeURIComponent(caseId)}/contact`,
    );
    return body.contact;
  } catch (caught) {
    if (caught instanceof ApiError && caught.status === 404 && caught.code === "reporter contact not found") {
      return null;
    }
    throw caught;
  }
}

// One audit entry. Actor and details are null for a caller without full case access.
export interface HistoryEntry {
  id: string;
  action: string;
  created_at: string;
  actor_id: string | null;
  details: Record<string, unknown> | null;
}

export interface HistoryPage {
  items: HistoryEntry[];
  total: number;
  limit: number;
  offset: number;
}

export function getCaseHistory(caseId: string, limit: number, offset: number): Promise<HistoryPage> {
  return request<HistoryPage>(`/cases/${encodeURIComponent(caseId)}/history?limit=${limit}&offset=${offset}`);
}

// Evidence. The list, download and custody log need evidence:read. Upload needs evidence:write. Both also need the case to be visible.
export interface EvidenceItem {
  id: string;
  case_id: string;
  uploaded_by: string;
  original_filename: string;
  media_type: string;
  size_bytes: number;
  sha256_plaintext: string | null;
  status: string;
  created_at: string;
}

export interface EvidencePage {
  items: EvidenceItem[];
  total: number;
  limit: number;
  offset: number;
}

export interface CustodyEvent {
  id: string;
  action: string;
  actor_id: string | null;
  details: Record<string, unknown>;
  created_at: string;
}

export function listEvidence(caseId: string, limit: number, offset: number): Promise<EvidencePage> {
  return request<EvidencePage>(`/cases/${encodeURIComponent(caseId)}/evidence?limit=${limit}&offset=${offset}`);
}

export function uploadEvidence(caseId: string, file: File): Promise<EvidenceItem> {
  const form = new FormData();
  form.append("file", file);
  return request<EvidenceItem>(`/cases/${encodeURIComponent(caseId)}/evidence`, { method: "POST", form });
}

// Each successful download is recorded in the custody log before the bytes are returned.
export function downloadEvidence(evidenceId: string): Promise<Blob> {
  return request<Blob>(`/evidence/${encodeURIComponent(evidenceId)}/download`, { responseType: "blob" });
}

export function listCustodyEvents(evidenceId: string): Promise<CustodyEvent[]> {
  return request<CustodyEvent[]>(`/evidence/${encodeURIComponent(evidenceId)}/custody`);
}

// Case record: awareness events, impacts, legal duties, classification and the preliminary report.
// Reads need a case-read permission and a visible case. Writes need cases:triage and an open case (409 when closed).
// Free text is null for callers without cases:read_all. The preliminary report needs cases:read_all.
export const AWARENESS_KINDS = ["operator", "affected_party", "external_report"] as const;
export type AwarenessKind = (typeof AWARENESS_KINDS)[number];

// An incident, a near miss and a blocked attempt are kept apart. The same values apply to the classification.
export const RECORD_EVENT_KINDS = ["incident", "near_miss", "blocked_attempt"] as const;
export type RecordEventKind = (typeof RECORD_EVENT_KINDS)[number];

export interface AwarenessEvent {
  id: string;
  case_id: string;
  awareness_kind: AwarenessKind;
  event_kind: RecordEventKind;
  occurred_at: string;
  // Null for callers without cases:read_all.
  basis: string | null;
  recorded_by: string;
  created_at: string;
}

export interface AwarenessEventCreateBody {
  awareness_kind: AwarenessKind;
  event_kind: RecordEventKind;
  // Timezone-aware UTC ISO string. The backend rejects a time in the future.
  occurred_at: string;
  basis: string;
}

export function listAwarenessEvents(caseId: string): Promise<AwarenessEvent[]> {
  return request<AwarenessEvent[]>(`/cases/${encodeURIComponent(caseId)}/awareness-events`);
}

// Awareness events are append-only. Each call adds one event.
export function addAwarenessEvent(caseId: string, body: AwarenessEventCreateBody): Promise<AwarenessEvent> {
  return request<AwarenessEvent>(`/cases/${encodeURIComponent(caseId)}/awareness-events`, {
    method: "POST",
    json: body,
  });
}

export const IMPACT_AREAS = [
  "affected_people_groups",
  "severity",
  "scale",
  "duration",
  "reversibility",
  "disproportionate_effect",
] as const;
export type ImpactArea = (typeof IMPACT_AREAS)[number];

export const IMPACT_SEVERITIES = ["none", "low", "medium", "high", "critical", "unknown"] as const;
export type ImpactSeverity = (typeof IMPACT_SEVERITIES)[number];

export interface ImpactAssessment {
  area: ImpactArea;
  // False when the area has no assessment yet. That is not the same as severity "none".
  assessed: boolean;
  severity: ImpactSeverity | null;
  // Null for callers without cases:read_all, and for an area that is not assessed.
  scope: string | null;
  updated_at: string | null;
}

// Always the six impact areas, in order. An area without an assessment comes back with assessed false.
export function listImpacts(caseId: string): Promise<ImpactAssessment[]> {
  return request<ImpactAssessment[]>(`/cases/${encodeURIComponent(caseId)}/impacts`);
}

export function setImpact(
  caseId: string,
  area: ImpactArea,
  body: { severity: ImpactSeverity; scope: string },
): Promise<ImpactAssessment> {
  return request<ImpactAssessment>(`/cases/${encodeURIComponent(caseId)}/impacts/${encodeURIComponent(area)}`, {
    method: "PUT",
    json: body,
  });
}

export const DUTY_KINDS = ["statutory", "contractual"] as const;
export type DutyKind = (typeof DUTY_KINDS)[number];
export type DutyStatus = "open" | "done";

// A legal duty has its own due time. It never changes a SAFE deadline task of the case.
export interface LegalDuty {
  id: string;
  case_id: string;
  kind: DutyKind;
  // Null for callers without cases:read_all.
  description: string | null;
  source_reference: string | null;
  jurisdiction: string | null;
  due_at: string;
  status: DutyStatus;
  created_by: string;
  created_at: string;
}

export interface LegalDutyCreateBody {
  kind: DutyKind;
  description: string;
  // The statute or the contract clause that creates the duty.
  source_reference: string;
  jurisdiction?: string | null;
  due_at: string;
}

// Only the sent fields change. A field sent as null is refused, except jurisdiction, which may be cleared.
export type LegalDutyChanges = Partial<{
  description: string;
  source_reference: string;
  jurisdiction: string | null;
  due_at: string;
  status: DutyStatus;
}>;

export function listLegalDuties(caseId: string): Promise<LegalDuty[]> {
  return request<LegalDuty[]>(`/cases/${encodeURIComponent(caseId)}/legal-duties`);
}

export function addLegalDuty(caseId: string, body: LegalDutyCreateBody): Promise<LegalDuty> {
  return request<LegalDuty>(`/cases/${encodeURIComponent(caseId)}/legal-duties`, { method: "POST", json: body });
}

export function updateLegalDuty(caseId: string, dutyId: string, changes: LegalDutyChanges): Promise<LegalDuty> {
  return request<LegalDuty>(
    `/cases/${encodeURIComponent(caseId)}/legal-duties/${encodeURIComponent(dutyId)}`,
    { method: "PATCH", json: changes },
  );
}

// The five compact trigger values of R01, and none_applies, which records that no trigger applies and stands alone.
export const TRIGGERS = [
  "unauthorized_access_or_change",
  "boundary_escape",
  "confidential_data_exposure",
  "out_of_scope_continuation",
  "harm_inaction_near_miss",
  "none_applies",
] as const;
export type Trigger = (typeof TRIGGERS)[number];

export const CLASSIFICATION_TEXT_FIELDS = [
  "classification_rationale",
  "responsible_operator",
  "affected_versions",
  "approved_scope",
  "scope_permission_source",
  "containment",
  "evidence_gap_plan",
] as const;
export type ClassificationTextField = (typeof CLASSIFICATION_TEXT_FIELDS)[number];
export type ClassificationText = Record<ClassificationTextField, string | null>;

// Version 0 means no classification is recorded yet. Null text is "not recorded", or hidden when the caller lacks cases:read_all.
export interface Classification extends ClassificationText {
  case_id: string;
  version: number;
  triggers: Trigger[];
  event_kind: RecordEventKind | null;
  internal_incident_option: boolean;
  next_update_due_at: string | null;
  created_by: string | null;
  created_at: string | null;
}

// Every field is sent. The backend stores the body as the next version, and a field not sent is cleared.
export type ClassificationUpdateBody = ClassificationText & {
  // The version the editor read. A different current version is refused with 409.
  expected_version: number;
  triggers: Trigger[];
  event_kind: RecordEventKind | null;
  internal_incident_option: boolean;
  next_update_due_at: string | null;
};

export function getClassification(caseId: string): Promise<Classification> {
  return request<Classification>(`/cases/${encodeURIComponent(caseId)}/classification`);
}

export function updateClassification(caseId: string, body: ClassificationUpdateBody): Promise<Classification> {
  return request<Classification>(`/cases/${encodeURIComponent(caseId)}/classification`, { method: "PUT", json: body });
}

// The report lists what is still missing and never refuses for it. The contact is reported as recorded or missing only.
export interface PreliminaryReport {
  case_id: string;
  reference: string;
  case_status: string;
  generated_at: string;
  awareness_time: string;
  complete: boolean;
  // Each entry names a field, such as "contact", "awareness_events", "impacts.severity" or "classification.containment".
  missing_fields: { field: string; state: "missing" | "unknown" }[];
  contact: { status: "recorded" | "missing" };
  classification: ClassificationText & {
    version: number;
    triggers: Trigger[];
    event_kind: RecordEventKind | null;
    internal_incident_option: boolean;
    next_update_due_at: string | null;
  };
  awareness_events: {
    id: string;
    awareness_kind: AwarenessKind;
    event_kind: RecordEventKind;
    occurred_at: string;
    basis: string;
  }[];
  impacts: { area: ImpactArea; assessed: boolean; severity: ImpactSeverity | null; scope: string | null }[];
  legal_duties: {
    id: string;
    kind: DutyKind;
    description: string;
    source_reference: string;
    jurisdiction: string | null;
    due_at: string;
    status: DutyStatus;
  }[];
  // The SAFE deadline tasks of the case, kept apart from the legal duties.
  safe_deadlines: { kind: string; due_at: string; status: string }[];
}

export function getPreliminaryReport(caseId: string): Promise<PreliminaryReport> {
  return request<PreliminaryReport>(`/cases/${encodeURIComponent(caseId)}/preliminary-report`);
}

// System timezone. Storage is always UTC; this only changes how times are displayed.
export interface SystemTimezone {
  timezone: string | null;
  effective_from: string | null;
  utc_offset: string | null;
}

export interface SystemTimezoneUpdate {
  timezone: string;
  // Explicit confirmation of the display change.
  confirm: boolean;
  // The timezone the admin was looking at. The backend rejects the change if the stored value differs.
  current: string | null;
}

export function getSystemTimezone(): Promise<SystemTimezone> {
  return request<SystemTimezone>("/system/timezone");
}

export function updateSystemTimezone(body: SystemTimezoneUpdate): Promise<SystemTimezone> {
  return request<SystemTimezone>("/system/timezone", { method: "PUT", json: body });
}

// Metadata about personal data stored by this installation. Row contents and secrets are never included.
export interface PrivacyInventoryItem {
  table: string;
  description: string;
  category: string;
  contains_personal_data: boolean;
  personal_fields: string[];
  purpose: string;
  storage: string;
  retention: string;
  retention_defined: boolean;
  security: string;
  deletion_mode: string;
  rows: { total: number; active: number; soft_deleted: number };
  oldest_created_at: string | null;
  newest_created_at: string | null;
}

export interface PrivacyInventory {
  generated_at: string;
  items: PrivacyInventoryItem[];
}

export type PrivacyExportFormat = "json" | "csv";

export function getPrivacyInventory(): Promise<PrivacyInventory> {
  return request<PrivacyInventory>("/privacy/inventory");
}

// Downloads the inventory as a file. The bytes come through request(), so bearer auth and refresh apply.
export async function downloadPrivacyExport(format: PrivacyExportFormat): Promise<void> {
  const blob = await request<Blob>(`/privacy/inventory/export?format=${format}`, { responseType: "blob" });
  saveBlob(blob, `privacy-inventory.${format}`);
}

// Hands a downloaded body to the browser as a file save.
export function saveBlob(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  // Revoke after the browser has started the download.
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

export async function readiness(): Promise<Readiness> {
  const response = await fetch("/api/v1/health/ready");
  if (response.ok) return "ready";
  try {
    const body = (await response.json()) as { detail?: { status?: string } };
    if (body.detail?.status === "setup_required") return "setup_required";
  } catch {
    // Fall through to unavailable.
  }
  return "unavailable";
}
