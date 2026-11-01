import { ApiError } from "./api";

const MESSAGES: Record<string, string> = {
  "invalid credentials": "Invalid username or password.",
  "too many login attempts": "Too many login attempts. Wait 15 minutes and try again.",
  setup_required: "This instance has no admin yet. An operator must create the first admin on the server.",
  session_expired: "Your session has expired. Please log in again.",
  "password change required": "You must change your password before continuing.",
  "current password is incorrect": "The current password is incorrect.",
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
  http_422: "Check the values you entered.",
};

export function describeCode(code: string): string {
  return MESSAGES[code] ?? `Request failed (${code}).`;
}

export function describeError(error: unknown): string {
  if (error instanceof ApiError) return describeCode(error.code);
  if (error instanceof TypeError) return "The server could not be reached.";
  return "Something went wrong.";
}
