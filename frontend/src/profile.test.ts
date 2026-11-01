import { describe, expect, it } from "vitest";
import { changedProfileFields, profileToFields, ProfileFields } from "./profile";

const SAVED: ProfileFields = {
  first_name: "Ada",
  last_name: "Lovelace",
  title: "Analyst",
  email: "ada@example.com",
};

describe("profileToFields", () => {
  it("maps null optional values to empty inputs", () => {
    expect(
      profileToFields({ first_name: null, last_name: null, title: null, email: "ada@example.com" }),
    ).toEqual({ first_name: "", last_name: "", title: "", email: "ada@example.com" });
  });

  it("keeps values that are present", () => {
    expect(profileToFields({ first_name: "Ada", last_name: "Lovelace", title: "Analyst", email: "ada@example.com" })).toEqual(
      SAVED,
    );
  });
});

describe("changedProfileFields", () => {
  it("sends nothing when nothing changed", () => {
    expect(changedProfileFields(SAVED, { ...SAVED })).toEqual({});
  });

  it("sends only the fields that changed", () => {
    expect(changedProfileFields(SAVED, { ...SAVED, email: "ada.l@example.com" })).toEqual({ email: "ada.l@example.com" });
  });

  it("trims the new values before sending them", () => {
    expect(changedProfileFields(SAVED, { ...SAVED, first_name: "  Grace  " })).toEqual({ first_name: "Grace" });
  });

  it("does not send a field whose only change is whitespace", () => {
    expect(changedProfileFields(SAVED, { ...SAVED, last_name: " Lovelace " })).toEqual({});
  });

  it("sends an empty string to clear an optional field", () => {
    expect(changedProfileFields(SAVED, { ...SAVED, title: "   " })).toEqual({ title: "" });
  });

  it("does not compare an untrimmed initial value against a trimmed one as a change", () => {
    const initial: ProfileFields = { ...SAVED, title: " Analyst " };
    expect(changedProfileFields(initial, { ...SAVED, title: "Analyst" })).toEqual({});
  });
});
