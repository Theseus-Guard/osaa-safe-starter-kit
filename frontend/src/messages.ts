import { ApiError } from "./api";

const MESSAGES: Record<string, string> = {
  "invalid credentials": "Invalid username or password.",
  "too many login attempts": "Too many login attempts. Wait 15 minutes and try again.",
  setup_required: "This instance has no admin yet. An operator must create the first admin on the server.",
  session_expired: "Your session has expired. Please log in again.",
  csrf_token_invalid: "The request could not be verified. Reload the page and try again.",
  "password change required": "You must change your password before continuing.",
  "current password is incorrect": "The current password is incorrect.",
  password_reused:
    "Choose a password you have not used recently. It must differ from your temporary password and your last three passwords.",
  "username already exists": "That username is already taken.",
  forbidden: "You do not have permission for this action.",
  "user not found": "No user with that ID exists.",
  "cannot change your own role or status": "You cannot change your own role or status.",
  "the last active admin cannot be removed": "The last active admin cannot be demoted or disabled.",
  "the protected admin account cannot be disabled, deleted, or demoted": "The protected admin account cannot be disabled, deleted, or demoted.",
  "the admin role must keep users:manage, users:reset_password and roles:manage":
    "The admin role must keep users:manage, users:reset_password and roles:manage.",
  "unknown permission": "One of the selected permissions does not exist.",
  "role not found": "That role does not exist.",
  "system setup required": "System setup required. An administrator must set the system timezone before you can continue.",
  "timezone changed by someone else; reload": "The timezone was changed by someone else. Reload the page to see the current value.",
  "confirmation required": "Confirm the change before saving.",
  "system timezone is not configured": "The system timezone is not set yet. An administrator must set it before cases can be created.",
  "case not found": "No case with that ID is available to your account.",
  "email is required": "Enter an email address.",
  "invalid email address": "Enter a valid email address.",
  "email already in use": "That email address is already used by another account.",
  "use your own profile endpoint": "Edit your own profile on the Account page.",
  "nothing to change": "Choose a role or status change first.",
  "username is reserved": "That username is reserved. Choose another.",
  "username must be 3-64 characters: lowercase letters, digits, '.', '_' or '-'":
    "Usernames must be 3-64 characters: lowercase letters, digits, '.', '_' or '-'.",
  "case is closed": "This case is closed.",
  "case is not closed": "This case is not closed, so it cannot be reopened.",
  classification_version_conflict:
    "The classification was changed by someone else. Reload it, check the current version and save again.",
  impact_version_conflict:
    "The impact values were changed by someone else. Reload them, check the current version and save again.",
  reporter_contact_not_grantable: "The reporter contact permission cannot be granted to any role.",
  reporting_owner_fixed: "The reporting_owner role is fixed. Its permissions cannot be changed.",
  reporter_contact_window_closed:
    "The reporter contact can no longer be read. It is available while the case is open and for 72 hours after it was closed.",
  "only an assigned reviewer can record a review": "Only an assigned reviewer can record a review on this case.",
  "review round is closed": "This review is closed, so its layers cannot change.",
  "review not found": "That review does not exist, or your account cannot see its case.",
  revision_conflict: "The review was changed by someone else. Reload it, check the current revision and save again.",
  "a conflict is declared on this review round, so this reviewer cannot set a layer":
    "A conflict is declared on this review round, so you cannot set its layers.",
  "this reviewer has withdrawn from this review round, so cannot set a layer":
    "You have withdrawn from this review round, so you cannot set its layers.",
  "only an assigned reviewer can declare a conflict": "Only an assigned reviewer can declare a conflict on this review round.",
  "only an assigned reviewer can withdraw from a review round":
    "Only an assigned reviewer can withdraw from this review round.",
  conflict_already_declared: "You have already declared a conflict on this review round. Reload the round before you change it.",
  no_declared_conflict: "You have no declared conflict on this review round to withdraw.",
  already_recused: "You have already withdrawn from this review round.",
  supersedes_mismatch: "The entry you are correcting is no longer the current one. Reload the round and try again.",
  "a reviewer cannot name themselves as the replacement": "Choose another person as the replacement.",
  "the replacement must be an active, assigned user who holds reviews:write":
    "The replacement must be an active reviewer assigned to this case.",
  "the replacement has a declared conflict on this review round": "The replacement has a declared conflict on this review round.",
  "the replacement has withdrawn from this review round": "The replacement has withdrawn from this review round.",
  no_eligible_reviewer:
    "No independent reviewer can be named for this round yet. An assigned reviewer who did not take part in the round and has no declared conflict on it is needed.",
  "evidence is not available on this case": "One of the evidence items is not available on this case.",
  "a pass needs at least one evidence item of this case": "A pass needs at least one evidence item of this case.",
  idempotency_key_required: "The request needs an Idempotency-Key. Try again.",
  idempotency_key_invalid: "The Idempotency-Key is not valid. Try again.",
  idempotency_key_reused: "This request key was already used for a different request. Try again with a new request.",
  case_closed: "This case is closed, so its disclosure packages cannot change.",
  version_conflict: "The item changed, so nothing was saved. Reload it, check the current version and save again.",
  no_change: "The package content did not change, so no new version was saved.",
  evidence_not_eligible:
    "One or more evidence items cannot be used. Each must be a redacted derivative of this case that you may see.",
  audience_wider_than_evidence: "The TLP audience is wider than one of the evidence items allows.",
  not_latest_version: "A newer version of this package exists. Only its latest version can be approved.",
  already_approved: "This version already has an approval.",
  preparer_is_approver: "You prepared this version, so another approver must approve it.",
  approval_binding_mismatch:
    "The recipient, purpose or digest does not match this version. Copy them exactly as they are shown.",
  evidence_not_visible: "One or more items are outside your TLP audience, so you cannot approve this version.",
  digest_mismatch: "The stored content does not match its digest, so nothing was approved. Tell an administrator.",
  "notice not found": "That notice does not exist on this disclosure version.",
  "disclosure version not found": "That disclosure version does not exist on this case.",
  manual_fallback_required:
    "Your account can record a notice only on the manual path. Choose the manual path and give the reason.",
  repeat_reason_required: "This recipient already has a notice for this version. Give a reason for the second notice.",
  time_out_of_range:
    "The time is before the approval or the send, or it is in the future. Enter the time it happened.",
  version_not_approved: "This version is not approved, so no notice can be recorded for it.",
  version_superseded:
    "A newer version of this package exists, so no notice can be recorded for this one. Use the latest approved version.",
  approval_mismatch: "The approval does not match this version. Tell an administrator.",
  release_approval_missing: "A CLEAR item has no release approval, so no notice can be recorded for this version.",
  release_trace_missing:
    "No release attempt is open for this version, so the release was refused. Start the release again from this version.",
  state_conflict: "The notice is no longer in a state that allows this change. Reload it and check its state.",
  "deadline task not found": "That deadline task does not exist on this case.",
  "legal duty not found": "That legal duty does not exist on this case.",
  "only a triager may mark a deadline not applicable": "Only a triager can mark a deadline not applicable.",
  "only a triager may restore a deadline that is not applicable":
    "Only a triager can restore a deadline that is not applicable.",
  deadline_not_marked_not_applicable: "This deadline is not marked not applicable, so there is nothing to restore.",
  "assignment not found": "That person is not assigned to this case.",
  "assignee is not an active user": "That user is not active, so they cannot be assigned.",
  "assignee must be an investigator or reviewer": "Only an investigator or a reviewer can be assigned to a case.",
  "user is already assigned to this case": "That user is already assigned to this case.",
  "cases are already linked": "These two cases are already linked.",
  "a case cannot be linked to itself": "Choose a different case to link to.",
  "link not found": "These two cases are not linked.",
  "evidence storage is not configured": "Evidence storage is not configured on this installation.",
  "evidence not found": "No evidence with that ID is available to your account.",
  "redaction is supported for text/plain evidence only": "Only text/plain evidence can be redacted.",
  "removed line ranges must be inside the document, in order, and not overlap":
    "The line ranges must be inside the document, in the order given, and must not overlap.",
  "the TLP label must be at least as restrictive as the original's label":
    "The copy's TLP label must be at least as restrictive as the original's label.",
  "the redaction expired before it finished; retry": "The redaction timed out before it finished. Try again.",
  "catalog entry not found": "No catalog entry with that ID is available to your account.",
  entry_closed: "This entry is withdrawn or superseded. It accepts no further changes.",
  "verification run is not known": "The verification run could not be found.",
  "replacement must be another open catalog entry": "Choose another open catalog entry as the replacement.",
  invalid_cursor: "The list position is not valid. Reload the list.",
  "evidence is not available": "This evidence is not available for download yet.",
  "evidence failed its integrity check": "The evidence failed its integrity check. Nothing was returned. The failure is in the custody log.",
  "evidence could not be stored": "The evidence could not be stored. Try the upload again.",
  "the upload expired before it finished; retry": "The upload timed out before it finished. Try again.",
  "media type is not allowed": "This file type is not allowed. Allowed types are PDF, PNG, JPEG, plain text and JSON.",
  "file is empty": "The file is empty.",
  "malformed multipart body": "The upload could not be read. Try again.",
  "multipart field 'file' is required": "Choose a file to upload.",
  http_422: "Check the values you entered.",
};

// Codes that carry a variable part after a fixed prefix.
const PREFIXED_MESSAGES: [prefix: string, render: (rest: string) => string][] = [
  ["case cannot be closed: ", (blockers) => `The case cannot be closed yet: ${blockers}.`],
  ["file exceeds the limit of ", (limit) => `The file is larger than the limit of ${limit}.`],
  ["request exceeds the limit of ", (limit) => `The upload is larger than the limit of ${limit}.`],
];

export function describeCode(code: string): string {
  if (Object.hasOwn(MESSAGES, code)) return MESSAGES[code];
  for (const [prefix, render] of PREFIXED_MESSAGES) {
    if (code.startsWith(prefix)) return render(code.slice(prefix.length));
  }
  return `Request failed (${code}).`;
}

export function describeError(error: unknown): string {
  if (error instanceof ApiError) return describeCode(error.code);
  if (error instanceof TypeError) return "The server could not be reached.";
  return "Something went wrong.";
}
