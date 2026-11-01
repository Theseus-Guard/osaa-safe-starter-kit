import { describe, expect, it } from "vitest";
import { ApiError } from "./api";
import { describeCode, describeError } from "./messages";

describe("describeCode", () => {
  it("returns the mapped sentence for a known code", () => {
    expect(describeCode("invalid credentials")).toBe("Invalid username or password.");
    expect(describeCode("setup_required")).toMatch(/no admin yet/);
  });

  it("falls back to the raw code for an unknown code", () => {
    expect(describeCode("something_new")).toBe("Request failed (something_new).");
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
