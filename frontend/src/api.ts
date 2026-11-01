// Thin client for the existing backend API, called through the gateway at /api/v1.
// Tokens live in memory only. A page reload requires logging in again.

export const ROLES = [
  "reporter",
  "triager",
  "investigator",
  "reviewer",
  "disclosure_approver",
  "auditor",
  "admin",
] as const;

export type Role = (typeof ROLES)[number];

export interface Me {
  id: string;
  username: string;
  role: Role;
  password_change_required: boolean;
  // True only for an admin when no system timezone is set yet.
  timezone_setup_required: boolean;
}

export interface CreatedUser {
  id: string;
  username: string;
  role: Role;
  password_change_required: boolean;
}

export interface UserSummary {
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

  constructor(status: number, code: string) {
    super(code);
    this.status = status;
    this.code = code;
  }
}

interface Session {
  access: string;
  refresh: string;
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT" | "PATCH" | "DELETE";
  json?: unknown;
  // Set to false for requests that must not carry or renew the session (login, readiness).
  auth?: boolean;
  // "blob" returns the raw body for file downloads. It goes through the same auth and refresh path.
  responseType?: "json" | "blob";
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
  session = { access: body.access_token, refresh: body.refresh_token };
}

async function send(path: string, options: RequestOptions): Promise<Response> {
  const headers = new Headers();
  if (options.json !== undefined) headers.set("Content-Type", "application/json");
  if (options.auth !== false && session) headers.set("Authorization", `Bearer ${session.access}`);
  return fetch(`/api/v1${path}`, {
    method: options.method ?? "GET",
    headers,
    body: options.json === undefined ? undefined : JSON.stringify(options.json),
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
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string") {
      code = body.detail;
    } else if (typeof body.detail === "object" && body.detail !== null && "status" in body.detail) {
      code = String((body.detail as { status: unknown }).status);
    }
  } catch {
    // Empty or non-JSON error body: keep the HTTP status code.
  }
  return new ApiError(response.status, code);
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
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

export function createUser(username: string, role: Role, temporaryPassword: string): Promise<CreatedUser> {
  return request<CreatedUser>("/users", {
    method: "POST",
    json: { username, role, temporary_password: temporaryPassword },
  });
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
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `privacy-inventory.${format}`;
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
