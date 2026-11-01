import { ProfileUpdate, UserProfile } from "./api";

// Profile values as form inputs hold them: strings, with null shown as an empty input.
export interface ProfileFields {
  first_name: string;
  last_name: string;
  title: string;
  email: string;
}

export const EMAIL_REQUIRED_MESSAGE = "Enter an email address. It is required so you can be contacted and sign in with it later.";

export function profileToFields(profile: UserProfile): ProfileFields {
  return {
    first_name: profile.first_name ?? "",
    last_name: profile.last_name ?? "",
    title: profile.title ?? "",
    email: profile.email ?? "",
  };
}

// Sends only the fields that changed, trimmed. An empty optional value clears that field on the server.
export function changedProfileFields(initial: ProfileFields, current: ProfileFields): ProfileUpdate {
  const update: ProfileUpdate = {};
  for (const key of Object.keys(initial) as (keyof ProfileFields)[]) {
    const value = current[key].trim();
    if (value !== initial[key].trim()) update[key] = value;
  }
  return update;
}
