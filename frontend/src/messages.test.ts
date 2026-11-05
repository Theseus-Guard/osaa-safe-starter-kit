import { describe, expect, it } from "vitest";
import { ApiError } from "./api";
import { describeCode, describeError } from "./messages";

describe("describeCode", () => {
  it("returns the mapped sentence for a known code", () => {
    expect(describeCode("invalid credentials")).toBe("Invalid username or password.");
    expect(describeCode("setup_required")).toMatch(/no admin yet/);
  });

  it("explains a validation answer and a body that is too large", () => {
    expect(describeCode("http_400")).toBe("Check the values you entered.");
    expect(describeCode("payload_too_large")).toMatch(/larger than the server allows/);
  });

  it("falls back to the raw code for an unknown code", () => {
    expect(describeCode("something_new")).toBe("Request failed (something_new).");
  });

  it("explains a classification version conflict by its stable code", () => {
    expect(describeCode("classification_version_conflict")).toBe(
      "The classification was changed by someone else. Reload it, check the current version and save again.",
    );
    expect(describeCode("legal duty not found")).toBe("That legal duty does not exist on this case.");
  });

  it("explains a refused change to the fixed institution_owner role", () => {
    expect(describeCode("institution_owner_fixed")).toBe(
      "The institution_owner role is fixed. Its permissions cannot be changed.",
    );
  });

  it("explains a refused change to the fixed reporting_owner role", () => {
    expect(describeCode("reporting_owner_fixed")).toBe(
      "The reporting_owner role is fixed. Its permissions cannot be changed.",
    );
  });

  it("explains a reporter contact read refused after the 72-hour window", () => {
    expect(describeCode("reporter_contact_window_closed")).toMatch(/72 hours after it was closed/);
  });

  // Known bug in messages.ts: MESSAGES is a plain object, so inherited keys such as "constructor" are found
  // by the lookup and returned as a function instead of the fallback sentence. Remove ".fails" once fixed.
  it("does not match inherited object keys", () => {
    expect(describeCode("constructor")).toBe("Request failed (constructor).");
    expect(describeCode("toString")).toBe("Request failed (toString).");
  });
});

describe("describeError", () => {
  it("describes an ApiError through its code", () => {
    expect(describeError(new ApiError(403, "forbidden"))).toBe("You do not have permission for this action.");
  });

  it("reports a network failure for a TypeError", () => {
    expect(describeError(new TypeError("Failed to fetch"))).toBe("The server could not be reached.");
  });

  it("uses a generic message for anything else", () => {
    expect(describeError(new Error("boom"))).toBe("Something went wrong.");
    expect(describeError("boom")).toBe("Something went wrong.");
    expect(describeError(undefined)).toBe("Something went wrong.");
  });
});

describe("password messages", () => {
  it("explains a reused password without the raw code", () => {
    expect(describeCode("password_reused")).toBe(
      "Choose a password you have not used recently. It must differ from your temporary password and your last three passwords.",
    );
  });
});

describe("case and evidence messages", () => {
  it("maps the evidence codes to sentences", () => {
    expect(describeCode("evidence is not available")).toBe("This evidence is not available for download yet.");
    expect(describeCode("media type is not allowed")).toMatch(/Allowed types are PDF/);
  });

  it("shows the closure blockers the server lists", () => {
    expect(describeCode("case cannot be closed: no review is recorded; deadline task weekly_update is open")).toBe(
      "The case cannot be closed yet: no review is recorded; deadline task weekly_update is open.",
    );
  });

  it("explains the size limit without the raw code", () => {
    expect(describeCode("file exceeds the limit of 52428800 bytes")).toBe(
      "The file is larger than the limit of 52428800 bytes.",
    );
    expect(describeCode("request exceeds the limit of 52494144 bytes")).toBe(
      "The upload is larger than the limit of 52494144 bytes.",
    );
  });

  it("explains that only an assigned reviewer can record a review", () => {
    expect(describeCode("only an assigned reviewer can record a review")).toBe(
      "Only an assigned reviewer can record a review on this case.",
    );
  });

  it("keeps the fallback for an unknown code that only resembles a prefix", () => {
    expect(describeCode("case cannot be reopened")).toBe("Request failed (case cannot be reopened).");
  });
});
