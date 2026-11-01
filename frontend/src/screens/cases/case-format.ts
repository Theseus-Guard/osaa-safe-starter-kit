// Display helpers for the case screens. Pure functions, so they are tested without rendering.

// "preliminary_safe_report" -> "Preliminary safe report", "case.closed" -> "Case closed"
export function humanize(value: string): string {
  const words = value.replace(/[._]/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

const HISTORY_ACTION_LABELS: Record<string, string> = {
  "case.create": "Case created",
  "case.closed": "Case closed",
  "case.reopened": "Case reopened",
  "case.assignment_added": "Assignment added",
  "case.assignment_removed": "Assignment removed",
  "case.review_recorded": "Review recorded",
  "case.deadline_updated": "Deadline updated",
  "evidence.uploaded": "Evidence uploaded",
  "evidence.downloaded": "Evidence downloaded",
};

export function historyActionLabel(action: string): string {
  return Object.hasOwn(HISTORY_ACTION_LABELS, action) ? HISTORY_ACTION_LABELS[action] : humanize(action);
}

const CUSTODY_ACTION_LABELS: Record<string, string> = {
  uploaded: "Uploaded",
  downloaded: "Downloaded",
  verified: "Integrity verified",
  failed: "Integrity check failed",
};

export function custodyActionLabel(action: string): string {
  return Object.hasOwn(CUSTODY_ACTION_LABELS, action) ? CUSTODY_ACTION_LABELS[action] : humanize(action);
}

const EVIDENCE_STATUS_LABELS: Record<string, string> = {
  pending: "Pending",
  available: "Available",
  failed: "Failed",
};

export function evidenceStatusLabel(status: string): string {
  return Object.hasOwn(EVIDENCE_STATUS_LABELS, status) ? EVIDENCE_STATUS_LABELS[status] : humanize(status);
}

// Only the types the backend accepts. The server checks the content again on upload.
export const ACCEPTED_EVIDENCE_TYPES = ["application/pdf", "image/png", "image/jpeg", "text/plain", "application/json"];

export const ACCEPTED_EVIDENCE_TYPE_NAMES = "PDF, PNG, JPEG, plain text and JSON";

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

// The audit log stores free text as a length marker, never as the text itself.
function showValue(value: unknown): string {
  if (value === null || value === undefined) return "none";
  if (isRecord(value) && typeof value.length === "number") return `${value.length} characters`;
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

// Turns audit details into readable lines. A change {old, new} reads as "old -> new".
export function describeDetails(details: Record<string, unknown> | null): string[] {
  if (details === null) return [];
  return Object.entries(details).map(([key, value]) => {
    const label = humanize(key);
    if (isRecord(value) && ("old" in value || "new" in value)) {
      return `${label}: ${showValue(value.old)} -> ${showValue(value.new)}`;
    }
    return `${label}: ${showValue(value)}`;
  });
}
