// Thin client for the existing backend API, called through the gateway at /api/v1.
// The session is carried by cookies that the server sets: the access and refresh tokens are HttpOnly, so this module
// never reads or stores them. Every request is sent with credentials. The CSRF cookie is readable by the page, and its
// value is sent back in the X-CSRF-Token header of every state-changing request.

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

export type Readiness = "ready" | "setup_required" | "unavailable";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  // Set only on a classification_version_conflict (409): the version the server holds now.
  readonly currentVersion: number | null;
  // Set only on a revision_conflict (409) of a review round: the revision the server holds now.
  readonly currentRevision: number | null;

  constructor(status: number, code: string, currentVersion: number | null = null, currentRevision: number | null = null) {
    super(code);
    this.status = status;
    this.code = code;
    this.currentVersion = currentVersion;
    this.currentRevision = currentRevision;
  }
}

// Only the fact that a session started and when its cookies were issued. The tokens themselves are never in the page.
interface Session {
  // When the current cookie pair was issued. Both cookies start their lifetime at the same moment.
  issuedAt: number;
}

// The pair is renewed by the first request made this long after it was issued. The refresh token has the same
// lifetime as the access token, so renewing only after the access token expires would find the refresh token expired
// too. A session that is not used for the full refresh lifetime ends on its next request.
const RENEW_AFTER_MS = 5 * 60 * 1000;

const CSRF_HEADER = "X-CSRF-Token";
const UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

// The CSRF cookie is not HttpOnly, so the page can read it. Returns null when the cookie is absent.
function csrfToken(): string | null {
  const match = document.cookie.match(/(?:^|;\s*)safe_csrf=([^;]*)/);
  return match ? match[1] : null;
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  json?: unknown;
  // Set to false for requests that must not carry or renew the session (login, readiness).
  auth?: boolean;
  // "blob" returns the raw body for file downloads. It goes through the same auth and refresh path.
  responseType?: "json" | "blob";
  // Multipart body for file uploads. The browser sets the Content-Type with the boundary.
  form?: FormData;
  // Extra request headers, such as Idempotency-Key.
  headers?: Record<string, string>;
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

// The server has set the cookies on the login response. Only the time is kept here.
function startSession(): void {
  session = { issuedAt: Date.now() };
}

// Every request carries the cookies (credentials: "include"). A state-changing request also carries the CSRF value.
async function send(path: string, options: RequestOptions): Promise<Response> {
  const method = options.method ?? "GET";
  const headers = new Headers(options.headers);
  if (options.json !== undefined) headers.set("Content-Type", "application/json");
  if (UNSAFE_METHODS.has(method)) {
    const csrf = csrfToken();
    if (csrf !== null) headers.set(CSRF_HEADER, csrf);
  }
  let body: BodyInit | undefined;
  if (options.form !== undefined) body = options.form;
  else if (options.json !== undefined) body = JSON.stringify(options.json);
  return fetch(`/api/v1${path}`, {
    method,
    headers,
    body,
    credentials: "include",
  });
}

// The refresh token travels in its HttpOnly cookie, so the request has no body. The server sets new cookies on success.
// Callers decide whether a refresh is wanted. After a page reload there is no session in memory yet, so this does not check one.
async function renew(): Promise<boolean> {
  const response = await send("/auth/refresh", { method: "POST" });
  if (!response.ok) return false;
  startSession();
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
  let currentRevision: number | null = null;
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string") {
      code = body.detail;
    } else if (typeof body.detail === "object" && body.detail !== null && "status" in body.detail) {
      const detail = body.detail as { status: unknown; current_version?: unknown; current_revision?: unknown };
      code = String(detail.status);
      if (typeof detail.current_version === "number") currentVersion = detail.current_version;
      if (typeof detail.current_revision === "number") currentRevision = detail.current_revision;
    }
  } catch {
    // Empty or non-JSON error body: keep the HTTP status code.
  }
  return new ApiError(response.status, code, currentVersion, currentRevision);
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

// The login response also carries the tokens for API clients. The page ignores them: the cookies are the session.
export async function login(username: string, password: string): Promise<void> {
  await request<unknown>("/auth/token", {
    method: "POST",
    json: { username, password },
    auth: false,
  });
  startSession();
}

// Public: the server says whether it runs in demo mode with the synthetic dataset. The demo banner depends on it only.
export interface DemoStatus {
  demo: boolean;
}

export function demoStatus(): Promise<DemoStatus> {
  return request<DemoStatus>("/demo/status");
}

export function me(): Promise<Me> {
  return request<Me>("/auth/me");
}

// Start-up after a page load. The page cannot read the HttpOnly cookies, so it asks the server whether a session exists.
// A 200 from /auth/me starts the session. A 401 gets one refresh (shared with any refresh already in flight), then one
// more /auth/me. Any other result, including a failed refresh, resolves to null, which means the login page is shown.
// It does not end a session or notify the session listener, because no session was running.
export async function restoreSession(): Promise<Me | null> {
  let response = await send("/auth/me", { method: "GET" });
  if (response.status === 401) {
    if (!(await refreshOnce())) return null;
    response = await send("/auth/me", { method: "GET" });
  }
  if (!response.ok) return null;
  const current = (await response.json()) as Me;
  startSession();
  return current;
}

// The server clears the session cookies and revokes the session. The local state is cleared even when the request fails.
export async function logout(): Promise<void> {
  try {
    await request("/auth/logout", { method: "POST" });
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

// Risk. The impact values C (Confidentiality), B (Integrity) and A (Availability) are each 1 to 5, or null when not set.
// The risk score is their mean to one decimal place, and null until all three are set. Nothing is stored for the score.
export interface RiskFields {
  risk_score: number | null;
  impact_c: number | null;
  impact_b: number | null;
  impact_a: number | null;
}

// The case statuses the backend stores and accepts in the case list status filter (safe_kit.cases.CaseStatus).
export const CASE_STATUSES = ["open", "closed"] as const;
export type CaseStatus = (typeof CASE_STATUSES)[number];

export interface CaseSummary extends RiskFields {
  id: string;
  reference: string;
  title: string;
  awareness_time: string;
  status: string;
  created_at: string;
  // The earliest due date of the case's open deadline tasks, or null when none is open.
  next_deadline: string | null;
  // The time of the case's latest change.
  updated_at: string;
}

export interface CaseDetail extends RiskFields {
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
// status narrows the list to one case status. Omitted means every status.
export function listCases(limit: number, offset: number, q?: string, status?: CaseStatus): Promise<CasePage> {
  const term = q?.trim() ?? "";
  const search = term === "" ? "" : `&q=${encodeURIComponent(term)}`;
  const filter = status === undefined ? "" : `&status=${encodeURIComponent(status)}`;
  return request<CasePage>(`/cases?limit=${limit}&offset=${offset}${search}${filter}`);
}

export function getCase(caseId: string): Promise<CaseDetail> {
  return request<CaseDetail>(`/cases/${encodeURIComponent(caseId)}`);
}

// Every state-changing request sends a new Idempotency-Key, so a repeated submit returns the first answer.
function idempotencyHeaders(): Record<string, string> {
  return { "Idempotency-Key": crypto.randomUUID() };
}

export function createCase(body: CaseCreateBody): Promise<CaseDetail> {
  return request<CaseDetail>("/cases", { method: "POST", json: body, headers: idempotencyHeaders() });
}

// Reporter intake. Returns the same shape as createCase. The contact is stored for the independent reporting owner and never returned.
export function createReport(body: ReportCreateBody): Promise<CaseDetail> {
  return request<CaseDetail>("/reports", { method: "POST", json: body, headers: idempotencyHeaders() });
}

// The reason is stored on the case and shown after closure.
export function closeCase(caseId: string, reason: string): Promise<CaseDetail> {
  return request<CaseDetail>(`/cases/${encodeURIComponent(caseId)}/close`, { method: "POST", json: { reason }, headers: idempotencyHeaders() });
}

// Needs cases:close, the same permission as closing. The reason is required and stored with the case.
export function reopenCase(caseId: string, reason: string): Promise<CaseDetail> {
  return request<CaseDetail>(`/cases/${encodeURIComponent(caseId)}/reopen`, { method: "POST", json: { reason }, headers: idempotencyHeaders() });
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
    headers: idempotencyHeaders(),
  });
}

// Turns off the not applicable exception of one task: it goes back to the status it had before. Due dates do not change.
export function restoreDeadline(caseId: string, taskId: string): Promise<CaseDetail> {
  return request<CaseDetail>(
    `/cases/${encodeURIComponent(caseId)}/deadlines/${encodeURIComponent(taskId)}/restore`,
    { method: "POST", headers: idempotencyHeaders() },
  );
}

// The outcome of one review layer, as the backend accepts it. A layer that is not set has no outcome (null).
export type LayerOutcome = "pass" | "fail" | "insufficient_evidence" | "not_applicable";

// The eleven layers of the review template, in the order the plan lists them. The keys are the API's layer names.
export const REVIEW_LAYERS: { key: string; label: string }[] = [
  { key: "model", label: "Model" },
  { key: "instructions", label: "Instructions" },
  { key: "safeguards", label: "Safeguards" },
  { key: "tools", label: "Tools" },
  { key: "environment", label: "Environment" },
  { key: "monitoring", label: "Monitoring" },
  { key: "human-operations", label: "Human operations" },
  { key: "supply-chain", label: "Supply chain" },
  { key: "serving-path", label: "Serving path" },
  { key: "authorization-and-commitment", label: "Authorization and commitment" },
  { key: "workflow-composition", label: "Workflow composition" },
];

export interface ReviewLayer {
  layer: string;
  outcome: LayerOutcome | null;
  // Null when the caller may not read free text (cases:read_all), and when the layer has no reason.
  reason: string | null;
  evidence_ids: string[];
  set_at: string | null;
}

// A review of a case. It has no overall result: only its state, its revision, and its layers.
export interface ReviewRound {
  id: string;
  case_id: string;
  status: "open" | "closed";
  // Starts at 1 and increases by one for every change to a layer. Send it as expected_revision when changing a layer.
  revision: number;
  opened_at: string;
  closed_at: string | null;
  layers: ReviewLayer[];
}

export interface ReviewLayerBody {
  outcome: LayerOutcome;
  // Required for every outcome except pass. Omitted or null otherwise.
  reason?: string | null;
  evidence_ids?: string[];
  expected_revision: number;
}

// Opens a review round with all eleven layers not set. Needs reviews:write and an active assignment to the case.
// Sends a new Idempotency-Key, so a repeated submit of the same request returns the first round.
export function openReviewRound(caseId: string): Promise<ReviewRound> {
  return request<ReviewRound>("/reviews", {
    method: "POST",
    json: { case_id: caseId },
    headers: { "Idempotency-Key": crypto.randomUUID() },
  });
}

// The rounds of a case, newest first. Needs case read access. Answers 404 when the caller cannot see the case.
export function listReviewRounds(caseId: string): Promise<ReviewRound[]> {
  return request<ReviewRound[]>(`/reviews?case_id=${encodeURIComponent(caseId)}`);
}

// Sets one layer of a round. A stale expected_revision answers 409 with ApiError.currentRevision and changes nothing.
export function setReviewLayer(roundId: string, layer: string, body: ReviewLayerBody): Promise<ReviewRound> {
  return request<ReviewRound>(`/reviews/${encodeURIComponent(roundId)}/layers/${encodeURIComponent(layer)}`, {
    method: "PATCH",
    json: body,
    headers: idempotencyHeaders(),
  });
}

// A conflict entry of a review round. Entries are never edited: a correction is a new entry that names the entry it
// supersedes, and the newest entry of a reviewer on the round is the current one. description is null for callers
// who may not read free text (cases:read_all, or the reviewer concerned).
export interface ReviewConflict {
  id: string;
  round_id: string;
  reviewer_id: string;
  kind: "declared" | "withdrawn";
  description: string | null;
  supersedes_id: string | null;
  created_at: string;
  current: boolean;
}

export interface ReviewConflictBody {
  kind: "declared" | "withdrawn";
  description?: string | null;
  supersedes_id?: string | null;
}

// A recusal of a reviewer from a round, with the replacement the reviewer names. reason is null for callers who may
// not read free text.
export interface ReviewRecusal {
  id: string;
  round_id: string;
  reviewer_id: string;
  replacement_id: string;
  reason: string | null;
  supersedes_id: string | null;
  created_at: string;
  current: boolean;
}

export interface ReviewRecusalBody {
  replacement_id: string;
  reason?: string | null;
  supersedes_id?: string | null;
}

// An appeal against a round. The product does not decide it. grounds is null for callers who may not read free text.
export interface ReviewAppeal {
  id: string;
  round_id: string;
  filed_by: string;
  independent_reviewer_id: string;
  grounds: string | null;
  created_at: string;
}

// The filer names no reviewer: the system names the independent reviewer.
export interface ReviewAppealBody {
  grounds: string;
}

// The conflict entries of a round, newest first. Answers 404 when the caller cannot see the round.
export function listReviewConflicts(roundId: string): Promise<ReviewConflict[]> {
  return request<ReviewConflict[]>(`/reviews/${encodeURIComponent(roundId)}/conflicts`);
}

// Declares, withdraws or corrects the caller's own conflict on a round. Sends a new Idempotency-Key.
export function declareReviewConflict(roundId: string, body: ReviewConflictBody): Promise<ReviewConflict> {
  return request<ReviewConflict>(`/reviews/${encodeURIComponent(roundId)}/conflicts`, {
    method: "POST",
    json: body,
    headers: { "Idempotency-Key": crypto.randomUUID() },
  });
}

// The recusals of a round, newest first. Answers 404 when the caller cannot see the round.
export function listReviewRecusals(roundId: string): Promise<ReviewRecusal[]> {
  return request<ReviewRecusal[]>(`/reviews/${encodeURIComponent(roundId)}/recusals`);
}

// Withdraws the caller from a round and names the replacement. Sends a new Idempotency-Key.
export function recuseFromReviewRound(roundId: string, body: ReviewRecusalBody): Promise<ReviewRecusal> {
  return request<ReviewRecusal>(`/reviews/${encodeURIComponent(roundId)}/recusals`, {
    method: "POST",
    json: body,
    headers: { "Idempotency-Key": crypto.randomUUID() },
  });
}

// The appeals against a round, newest first. Answers 404 when the caller cannot see the round.
export function listReviewAppeals(roundId: string): Promise<ReviewAppeal[]> {
  return request<ReviewAppeal[]>(`/reviews/${encodeURIComponent(roundId)}/appeals`);
}

// Files an appeal against a round for an independent reviewer. Sends a new Idempotency-Key.
export function fileReviewAppeal(roundId: string, body: ReviewAppealBody): Promise<ReviewAppeal> {
  return request<ReviewAppeal>(`/reviews/${encodeURIComponent(roundId)}/appeals`, {
    method: "POST",
    json: body,
    headers: { "Idempotency-Key": crypto.randomUUID() },
  });
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
    headers: idempotencyHeaders(),
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
    headers: idempotencyHeaders(),
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
  // The TLP audience label. Null for an uploaded original, which has no TLP rule.
  tlp_label?: string | null;
}

// TLP audience labels, from the narrowest to the widest. A redacted copy may not be labelled less restrictively than its original.
export const TLP_LABELS = ["RED", "AMBER", "AMBER+STRICT", "GREEN", "CLEAR"] as const;
export type TlpLabel = (typeof TLP_LABELS)[number];

export interface RemovedRange {
  from_line: number;
  to_line: number;
  // The number of lines in the range, from 1-based inclusive line numbers.
  lines: number;
  reason: string;
}

// The manifest of a redacted copy. It names the removed ranges and the reasons, never the removed text.
export interface RedactionManifest {
  method: string;
  replacement: string;
  source_sha256: string | null;
  tlp_label: string;
  lines_total: number;
  removed_lines: number;
  removed: RemovedRange[];
}

// A review layer that cites the original. On the redacted copy it is insufficient_evidence.
export interface ClaimAssessment {
  review_layer_id: string;
  round_id: string;
  layer: string;
  outcome: string;
  reason: string;
}

export interface Derivation {
  redaction_id: string;
  original_evidence_id: string;
  derived: EvidenceItem;
  manifest: RedactionManifest;
  claim_assessments: ClaimAssessment[];
}

export interface RedactionRangeBody {
  from_line: number;
  to_line: number;
  reason: string;
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
  return request<EvidenceItem>(`/cases/${encodeURIComponent(caseId)}/evidence`, { method: "POST", form, headers: idempotencyHeaders() });
}

// Each successful download is recorded in the custody log before the bytes are returned.
export function downloadEvidence(evidenceId: string): Promise<Blob> {
  return request<Blob>(`/evidence/${encodeURIComponent(evidenceId)}/download`, { responseType: "blob" });
}

export function listCustodyEvents(evidenceId: string): Promise<CustodyEvent[]> {
  return request<CustodyEvent[]>(`/evidence/${encodeURIComponent(evidenceId)}/custody`);
}

// One item, with its TLP label. Needs evidence:read and a visible case. A TLP-hidden item answers the same 404 as a missing one.
export function getEvidence(evidenceId: string): Promise<EvidenceItem> {
  return request<EvidenceItem>(`/evidence/${encodeURIComponent(evidenceId)}`);
}

// The redacted copies of an original that the caller may see under the TLP rule.
export function listDerivatives(evidenceId: string): Promise<EvidenceItem[]> {
  return request<EvidenceItem[]>(`/evidence/${encodeURIComponent(evidenceId)}/derivatives`);
}

// The manifest and claim assessments of a redacted copy. An original answers 404.
export function getRedaction(derivedEvidenceId: string): Promise<Derivation> {
  return request<Derivation>(`/evidence/${encodeURIComponent(derivedEvidenceId)}/redaction`);
}

// Needs evidence:read and evidence:write. The original is unchanged; the copy is a new item with its own ID.
export function redactEvidence(
  evidenceId: string,
  body: { removed: RedactionRangeBody[]; tlp_label: TlpLabel },
): Promise<Derivation> {
  return request<Derivation>(`/evidence/${encodeURIComponent(evidenceId)}/derivatives`, {
    method: "POST",
    json: body,
    headers: idempotencyHeaders(),
  });
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
    headers: idempotencyHeaders(),
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
    headers: idempotencyHeaders(),
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
  return request<LegalDuty>(`/cases/${encodeURIComponent(caseId)}/legal-duties`, { method: "POST", json: body, headers: idempotencyHeaders() });
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
  next_update_due_at: string | null;
};

export function getClassification(caseId: string): Promise<Classification> {
  return request<Classification>(`/cases/${encodeURIComponent(caseId)}/classification`);
}

export function updateClassification(caseId: string, body: ClassificationUpdateBody): Promise<Classification> {
  return request<Classification>(`/cases/${encodeURIComponent(caseId)}/classification`, { method: "PUT", json: body, headers: idempotencyHeaders() });
}

// Risk impact values. Reads need a case-read permission. A save needs cases:triage and an open case (409 when closed).
export type ImpactValue = 1 | 2 | 3 | 4 | 5;

// Version 0 means no values are recorded. A value that is not set is null.
export interface ImpactScores {
  case_id: string;
  version: number;
  c: ImpactValue | null;
  b: ImpactValue | null;
  a: ImpactValue | null;
  risk_score: number | null;
  created_by: string | null;
  created_at: string | null;
}

// Every value is sent. The backend stores the body as the next version, and a value that is null is cleared.
export interface ImpactScoresUpdateBody {
  // The version the editor read. A different current version is refused with 409.
  expected_version: number;
  c: ImpactValue | null;
  b: ImpactValue | null;
  a: ImpactValue | null;
}

export function getImpactScores(caseId: string): Promise<ImpactScores> {
  return request<ImpactScores>(`/cases/${encodeURIComponent(caseId)}/impact-scores`);
}

// Sends a new Idempotency-Key, so a repeated submit of the same save returns the first answer.
export function updateImpactScores(caseId: string, body: ImpactScoresUpdateBody): Promise<ImpactScores> {
  return request<ImpactScores>(`/cases/${encodeURIComponent(caseId)}/impact-scores`, {
    method: "POST",
    json: body,
    headers: { "Idempotency-Key": crypto.randomUUID() },
  });
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

// Downloads the inventory as a file. The bytes come through request(), so the session cookies and refresh apply.
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

// Overview dashboard (F2). Counts and dates only. Every response is role-gated by the backend.
export const OVERVIEW_RANGES = [7, 30, 90] as const;
export type OverviewRange = (typeof OVERVIEW_RANGES)[number];
export const DEFAULT_OVERVIEW_RANGE: OverviewRange = 30;

export interface OwnCaseCards {
  open_cases: number;
  opened_in_period: number;
  deadlines_due_72h: number;
  deadlines_overdue: number;
}

export interface CaseCards {
  open_cases: number;
  opened_in_period: number;
}

export type WorkerState = "ok" | "stale" | "no_data";

export interface OpsCards {
  failed_outbox_jobs: number;
  status: WorkerState;
  heartbeat_age_seconds: number | null;
}

// Disclosure versions waiting for approval (disclosure approver only). Counts and ages, no package content.
export interface ApprovalTemplateCounts {
  template: string;
  pending: number;
  oldest_pending_age_days: number | null;
}

export interface ApprovalCards {
  pending: number;
  oldest_pending_age_days: number | null;
  by_template: ApprovalTemplateCounts[];
}

export interface WeeklyStatusCounts {
  status: "open" | "closed";
  cases_with_significant_risk: number;
  cases_with_weekly_update: number;
  // YYYY-MM-DD in the system timezone, or null when no weekly update exists.
  latest_update_date: string | null;
}

// Investigator and reviewer only. A significant risk is an impact of high or critical severity.
export interface WeeklyUpdateCards {
  open_significant_risk_cases: number;
  latest_update_date: string | null;
  by_status: WeeklyStatusCounts[];
}

export interface OverviewSummary {
  days: number;
  timezone: string;
  own_cases: OwnCaseCards | null;
  case_activity: CaseCards | null;
  users: { pending_password_change: number } | null;
  ops: OpsCards | null;
  approvals: ApprovalCards | null;
  weekly_updates: WeeklyUpdateCards | null;
}

// The caller's own series: cases by status at the end of each day, and own audit actions per day.
export type SeriesMetric =
  | "cases_created"
  | "audit_events"
  | "logins_success"
  | "logins_failed"
  | "outbox_completed"
  | "outbox_failed"
  | "my_cases_open"
  | "my_cases_closed"
  | "my_actions_cases_opened"
  | "my_actions_assignments"
  | "my_actions_reviews"
  | "my_actions_cases_closed";

export interface SeriesPoint {
  date: string;
  count: number;
}

export interface SeriesResponse {
  metric: SeriesMetric;
  days: number;
  timezone: string;
  points: SeriesPoint[];
}

export interface DeadlineKindCounts {
  kind: string;
  open: number;
  overdue: number;
  done: number;
  not_applicable: number;
}

export interface DeadlineSummary {
  items: DeadlineKindCounts[];
}

export interface AuditSummary {
  days: number;
  timezone: string;
  // The top five actions, then "other" when there are more.
  actions: string[];
  points: { date: string; counts: Record<string, number> }[];
}

export interface UsersSummary {
  total: number;
  pending_password_change: number;
  roles: { role: string; active: number; inactive: number }[];
}

export interface OutboxSummary {
  counts: Record<string, number>;
  oldest_pending_age_seconds: number | null;
}

export function getOverviewSummary(days: OverviewRange): Promise<OverviewSummary> {
  return request<OverviewSummary>(`/overview/summary?days=${days}`);
}

export function getMetricSeries(metric: SeriesMetric, days: OverviewRange): Promise<SeriesResponse> {
  return request<SeriesResponse>(`/metrics/timeseries?metric=${metric}&days=${days}`);
}

export function getDeadlineSummary(): Promise<DeadlineSummary> {
  return request<DeadlineSummary>("/cases/deadlines/summary");
}

export function getAuditSummary(days: OverviewRange): Promise<AuditSummary> {
  return request<AuditSummary>(`/audit/summary?days=${days}`);
}

export function getUsersSummary(): Promise<UsersSummary> {
  return request<UsersSummary>("/users/summary");
}

export function getOutboxSummary(): Promise<OutboxSummary> {
  return request<OutboxSummary>("/ops/outbox-summary");
}

// Disclosure packages (plans/f0/api-draft.md section 2.5). Needs disclosures:read to read and disclosures:approve to approve,
// and the case must be visible. A version is never changed: a changed package is a new version.
export type DisclosureApprovalState = "approved" | "not_approved" | "revoked_by_change";

export interface DisclosureEvidence {
  // False for an item the caller may not see under the TLP rule. Its details are then absent.
  withheld: boolean;
  evidence_id?: string;
  media_type?: string;
  size_bytes?: number;
  tlp_label?: string;
  removed_lines?: number | null;
}

export interface DisclosureApproval {
  id: string;
  recipient: string;
  purpose: string;
  digest: string;
  preparer_id: string;
  approved_by: string;
  approved_at: string;
  separation_rule: string;
}

export interface DisclosureVersion {
  id: string;
  package_id: string;
  case_id: string;
  version: number;
  is_latest: boolean;
  template: string;
  tlp_audience: string;
  recipient: string;
  purpose: string;
  digest: string;
  approval_state: DisclosureApprovalState;
  approval: DisclosureApproval | null;
  evidence: DisclosureEvidence[];
  withheld_count: number;
  created_by: string;
  created_at: string;
}

export interface DisclosureSection {
  title: string;
  contains: string;
}

// The structure of one version. It holds no evidence content, no file name, and no redaction reason.
export interface DisclosurePreview {
  version_id: string;
  package_id: string;
  case_id: string;
  version: number;
  template: { key: string; title: string; sections: DisclosureSection[] };
  recipient: string;
  purpose: string;
  tlp_audience: string;
  digest: string;
  approval_state: DisclosureApprovalState;
  evidence: DisclosureEvidence[];
  withheld_count: number;
  notice: string;
}

export interface DisclosureList {
  items: DisclosureVersion[];
}

export interface DisclosureApprovalBody {
  recipient: string;
  purpose: string;
  digest: string;
}

export function listDisclosures(caseId: string): Promise<DisclosureList> {
  return request<DisclosureList>(`/cases/${encodeURIComponent(caseId)}/disclosures`);
}

export function getDisclosurePreview(caseId: string, versionId: string): Promise<DisclosurePreview> {
  return request<DisclosurePreview>(
    `/cases/${encodeURIComponent(caseId)}/disclosures/${encodeURIComponent(versionId)}/preview`,
  );
}

// The versioned export of one approved version (schemas/disclosure-export.v0.json). The API refuses a version that has
// no approval. The body holds no evidence content and is saved as a file by the caller.
export function exportDisclosure(caseId: string, versionId: string): Promise<Blob> {
  return request<Blob>(
    `/cases/${encodeURIComponent(caseId)}/disclosures/${encodeURIComponent(versionId)}/export`,
    { responseType: "blob" },
  );
}

// The approver confirms the recipient, purpose and digest of the version. The API refuses any other value.
export function approveDisclosure(
  caseId: string,
  versionId: string,
  body: DisclosureApprovalBody,
): Promise<DisclosureVersion> {
  return request<DisclosureVersion>(
    `/cases/${encodeURIComponent(caseId)}/disclosures/${encodeURIComponent(versionId)}/approvals`,
    { method: "POST", json: body, headers: idempotencyHeaders() },
  );
}

// External notices of a disclosure version (safe_kit.external_notices). A notice records a send that a person made. Nothing
// is sent from here, and a notice is never resent automatically. Recording needs disclosures:approve, or disclosures:prepare
// on the manual path with a reason. A receipt, follow-up or cancel is recorded with the command line for now.
export type NoticeChannel = "email" | "postal" | "webhook" | "secure_manual" | "other";
export type NoticeState =
  | "sent"
  | "receipt_received"
  | "receipt_unknown"
  | "follow_up_required"
  | "follow_up_done"
  | "cancelled";

export interface ExternalNotice {
  id: string;
  revision: number;
  case_id: string;
  package_id: string;
  version_id: string;
  version: number;
  channel: NoticeChannel;
  channel_other: string | null;
  // An identifier of the recipient kept outside SAFE. Not a name or an address.
  recipient_ref: string;
  sent_at: string;
  recorded_at: string;
  recorded_by: string;
  manual_fallback: boolean;
  fallback_reason: string | null;
  repeat_reason: string | null;
  state: NoticeState;
  state_reason_code: string | null;
  state_reason: string | null;
  received_at: string | null;
  changed_by: string;
  changed_at: string;
}

export interface ExternalNoticeList {
  items: ExternalNotice[];
}

export interface ExternalNoticeBody {
  channel: NoticeChannel;
  channel_other: string | null;
  recipient_ref: string;
  sent_at: string;
  manual_fallback: boolean;
  fallback_reason: string | null;
  repeat_reason: string | null;
}

export function listNotices(caseId: string, versionId: string): Promise<ExternalNoticeList> {
  return request<ExternalNoticeList>(
    `/cases/${encodeURIComponent(caseId)}/disclosures/${encodeURIComponent(versionId)}/notices`,
  );
}

export function recordNotice(caseId: string, versionId: string, body: ExternalNoticeBody): Promise<ExternalNotice> {
  return request<ExternalNotice>(
    `/cases/${encodeURIComponent(caseId)}/disclosures/${encodeURIComponent(versionId)}/notices`,
    { method: "POST", json: body, headers: idempotencyHeaders() },
  );
}

export async function readiness(): Promise<Readiness> {
  const response = await fetch("/api/v1/health/ready", { credentials: "include" });
  if (response.ok) return "ready";
  try {
    const body = (await response.json()) as { detail?: { status?: string } };
    if (body.detail?.status === "setup_required") return "setup_required";
  } catch {
    // Fall through to unavailable.
  }
  return "unavailable";
}

// Catalog. Reads need catalog:read; writes need catalog:write. A restricted reference and its digest are returned only
// with catalog:read_restricted. A withdrawn or superseded entry accepts no further change (409 entry_closed).
export const CATALOG_RESULTS = ["pass", "fail", "inconclusive"] as const;
export type CatalogResult = (typeof CATALOG_RESULTS)[number];

// What a catalog version shows of its verification run. The checker and the evidence path stay internal.
export interface VerificationSummary {
  run_id: string;
  subject: string;
  method: string;
  result: CatalogResult;
  verified_at: string;
}

export interface CatalogVersion {
  version: number;
  operation: "create" | "version" | "correction" | "dispute" | "withdrawal" | "supersession";
  status: string;
  title: string;
  body: string;
  reason: string | null;
  replacement_entry_id: string | null;
  created_by: string;
  created_at: string;
  verification: VerificationSummary | null;
  // True only for a passing run. A recommendation alone never makes an entry verified.
  verified: boolean;
  // Present only for a caller with catalog:read_restricted. Null when the version has none.
  restricted?: { reference: string; digest: string } | null;
}

export interface CatalogEntry {
  id: string;
  created_by: string;
  created_at: string;
  status: string;
  version: number;
  current: CatalogVersion;
  // The full history, oldest first. Present on the detail read only.
  versions?: CatalogVersion[];
}

export interface CatalogEntryPage {
  items: CatalogEntry[];
  next_cursor: string | null;
}

export interface Recommendation {
  id: string;
  failure_or_system: string;
  required_defence_outcome: string;
  minimum_control: string;
  control_alternative: string | null;
  responsible: string;
  due_at: string;
  evidence_to_keep: string;
  created_by: string;
  created_at: string;
}

export interface RecommendationPage {
  items: Recommendation[];
  next_cursor: string | null;
}

export interface RecommendationBody {
  failure_or_system: string;
  required_defence_outcome: string;
  minimum_control: string;
  control_alternative: string | null;
  responsible: string;
  due_at: string;
  evidence_to_keep: string;
}

export interface VerificationRunBody {
  subject: string;
  scope: string;
  assumptions: string;
  evidence_path: string;
  failure_behavior: string;
  error_class: string;
  uncertainty: string;
  method: string;
  result: CatalogResult;
}

export interface VerificationRun extends VerificationRunBody {
  id: string;
  recommendation_id: string;
  verified_by: string;
  verified_at: string;
}

// The fields of a new version or a correction. The expected version is the one the editor read.
export interface CatalogContentBody {
  title: string;
  body: string;
  verification_run_id?: string | null;
  restricted_reference?: string | null;
}

export function listCatalogEntries(cursor: string | null = null): Promise<CatalogEntryPage> {
  const query = cursor === null ? "" : `&cursor=${encodeURIComponent(cursor)}`;
  return request<CatalogEntryPage>(`/catalog/entries?limit=50${query}`);
}

export function getCatalogEntry(entryId: string): Promise<CatalogEntry> {
  return request<CatalogEntry>(`/catalog/entries/${encodeURIComponent(entryId)}`);
}

export function createCatalogEntry(body: CatalogContentBody): Promise<CatalogEntry> {
  return request<CatalogEntry>("/catalog/entries", { method: "POST", json: body, headers: idempotencyHeaders() });
}

export function addCatalogVersion(
  entryId: string,
  expectedVersion: number,
  body: CatalogContentBody,
): Promise<CatalogEntry> {
  return request<CatalogEntry>(`/catalog/entries/${encodeURIComponent(entryId)}/versions`, {
    method: "POST",
    json: { expected_version: expectedVersion, ...body },
    headers: idempotencyHeaders(),
  });
}

export function correctCatalogEntry(
  entryId: string,
  expectedVersion: number,
  body: CatalogContentBody & { reason: string },
): Promise<CatalogEntry> {
  return request<CatalogEntry>(`/catalog/entries/${encodeURIComponent(entryId)}/corrections`, {
    method: "POST",
    json: { expected_version: expectedVersion, ...body },
    headers: idempotencyHeaders(),
  });
}

// A dispute, or a withdrawal. Both keep the content and need a reason.
export function disputeCatalogEntry(entryId: string, expectedVersion: number, reason: string): Promise<CatalogEntry> {
  return request<CatalogEntry>(`/catalog/entries/${encodeURIComponent(entryId)}/disputes`, {
    method: "POST",
    json: { expected_version: expectedVersion, reason },
    headers: idempotencyHeaders(),
  });
}

export function withdrawCatalogEntry(entryId: string, expectedVersion: number, reason: string): Promise<CatalogEntry> {
  return request<CatalogEntry>(`/catalog/entries/${encodeURIComponent(entryId)}/withdrawals`, {
    method: "POST",
    json: { expected_version: expectedVersion, reason },
    headers: idempotencyHeaders(),
  });
}

export function supersedeCatalogEntry(
  entryId: string,
  expectedVersion: number,
  replacementEntryId: string,
  reason: string | null,
): Promise<CatalogEntry> {
  return request<CatalogEntry>(`/catalog/entries/${encodeURIComponent(entryId)}/supersessions`, {
    method: "POST",
    json: { expected_version: expectedVersion, replacement_entry_id: replacementEntryId, reason },
    headers: idempotencyHeaders(),
  });
}

export function listRecommendations(cursor: string | null = null): Promise<RecommendationPage> {
  const query = cursor === null ? "" : `&cursor=${encodeURIComponent(cursor)}`;
  return request<RecommendationPage>(`/catalog/recommendations?limit=50${query}`);
}

export function createRecommendation(body: RecommendationBody): Promise<Recommendation> {
  return request<Recommendation>("/catalog/recommendations", {
    method: "POST",
    json: body,
    headers: idempotencyHeaders(),
  });
}

export function recordVerificationRun(recommendationId: string, body: VerificationRunBody): Promise<VerificationRun> {
  return request<VerificationRun>(`/catalog/recommendations/${encodeURIComponent(recommendationId)}/verification-runs`, {
    method: "POST",
    json: body,
    headers: idempotencyHeaders(),
  });
}

// The verification of an entry, as the CLI does it: record the run, then save a version with the same content that
// names the run. The entry shows as verified only when the run passed. A failing run is attached too, so the history shows it.
export async function verifyCatalogEntry(
  entry: CatalogEntry,
  recommendationId: string,
  run: VerificationRunBody,
): Promise<CatalogEntry> {
  const recorded = await recordVerificationRun(recommendationId, run);
  return addCatalogVersion(entry.id, entry.version, {
    title: entry.current.title,
    body: entry.current.body,
    verification_run_id: recorded.id,
  });
}
