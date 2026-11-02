import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { hashFor, PAGE_INFO, PAGES, pageInNavigation, routeFromHash, useHashRoute } from "./routes";

// The router reads the URL hash, so each test starts from a known hash.
beforeEach(() => {
  window.location.hash = "";
});

afterEach(() => {
  window.location.hash = "";
});

describe("routeFromHash", () => {
  it("opens a plain page", () => {
    expect(routeFromHash("#/cases", false)).toEqual({ page: "cases", caseId: null, entryId: null });
    expect(routeFromHash("#/account", false)).toEqual({ page: "account", caseId: null, entryId: null });
  });

  it("reads the case id on the case detail page", () => {
    expect(routeFromHash("#/cases/case-42", false)).toEqual({ page: "cases", caseId: "case-42", entryId: null });
  });

  it("decodes an encoded case id", () => {
    expect(routeFromHash("#/cases/case%2F42", false)).toEqual({ page: "cases", caseId: "case/42", entryId: null });
  });

  it("ignores a trailing slash without an id", () => {
    expect(routeFromHash("#/cases/", false)).toEqual({ page: "cases", caseId: null, entryId: null });
  });

  it("ignores a second segment on pages other than cases", () => {
    expect(routeFromHash("#/account/extra", false)).toEqual({ page: "account", caseId: null, entryId: null });
  });

  it("accepts the hash without the trailing slash", () => {
    expect(routeFromHash("#cases", false)).toEqual({ page: "cases", caseId: null, entryId: null });
  });

  it("falls back to overview for an empty or unknown hash", () => {
    expect(routeFromHash("", false)).toEqual({ page: "overview", caseId: null, entryId: null });
    expect(routeFromHash("#/", false)).toEqual({ page: "overview", caseId: null, entryId: null });
    expect(routeFromHash("#/does-not-exist", false)).toEqual({ page: "overview", caseId: null, entryId: null });
  });

  it("sends a non-admin to overview for every admin-only page", () => {
    const adminOnly = PAGES.filter((page) => PAGE_INFO[page].adminOnly);
    expect(adminOnly).toEqual(expect.arrayContaining(["users", "privacy", "roles", "settings"]));
    for (const page of adminOnly) {
      expect(routeFromHash(`#/${page}`, false)).toEqual({ page: "overview", caseId: null, entryId: null });
    }
  });

  it("opens admin-only pages for an admin", () => {
    expect(routeFromHash("#/users", true)).toEqual({ page: "users", caseId: null, entryId: null });
    expect(routeFromHash("#/settings", true)).toEqual({ page: "settings", caseId: null, entryId: null });
  });
});

describe("hashFor", () => {
  it("builds the hash for a page", () => {
    expect(hashFor("users")).toBe("#/users");
  });

  it("builds the hash for a case with an encoded id", () => {
    expect(hashFor("cases", "case 42/a")).toBe("#/cases/case%2042%2Fa");
  });

  it("round-trips through routeFromHash", () => {
    const hash = hashFor("cases", "case 42");
    expect(routeFromHash(hash, false)).toEqual({ page: "cases", caseId: "case 42", entryId: null });
  });
});

describe("useHashRoute", () => {
  it("reads the current hash on mount", () => {
    window.location.hash = "#/cases/case-7";
    const { result } = renderHook(() => useHashRoute(false));
    expect(result.current).toEqual({ page: "cases", caseId: "case-7", entryId: null });
  });

  it("rewrites an unknown hash so the URL matches the page on screen", () => {
    window.location.hash = "#/nope";
    const { result } = renderHook(() => useHashRoute(false));
    expect(result.current).toEqual({ page: "overview", caseId: null, entryId: null });
    expect(window.location.hash).toBe("#/overview");
  });

  it("follows a hashchange event", () => {
    const { result } = renderHook(() => useHashRoute(false));
    act(() => {
      window.location.hash = "#/account";
      window.dispatchEvent(new HashChangeEvent("hashchange"));
    });
    expect(result.current).toEqual({ page: "account", caseId: null, entryId: null });
  });

  it("opens an admin-only page for an admin and rewrites it to overview when admin rights end", () => {
    window.location.hash = "#/users";
    const { result, rerender } = renderHook(({ isAdmin }: { isAdmin: boolean }) => useHashRoute(isAdmin), {
      initialProps: { isAdmin: true },
    });
    expect(result.current.page).toBe("users");
    rerender({ isAdmin: false });
    expect(result.current.page).toBe("overview");
    expect(window.location.hash).toBe("#/overview");
  });
});

describe("catalog page routes", () => {
  it("opens the catalog list and an entry by ID", () => {
    expect(routeFromHash("#/catalog", false)).toEqual({ page: "catalog", caseId: null, entryId: null });
    expect(routeFromHash("#/catalog/entry-42", false)).toEqual({ page: "catalog", caseId: null, entryId: "entry-42" });
    expect(routeFromHash("#/catalog/entry%2F42", false)).toEqual({ page: "catalog", caseId: null, entryId: "entry/42" });
  });

  it("builds the entry link with the ID encoded", () => {
    expect(hashFor("catalog", "entry/42")).toBe("#/catalog/entry%2F42");
    expect(hashFor("catalog")).toBe("#/catalog");
  });

  it("lists the catalog in the navigation only for a caller with catalog:read", () => {
    expect(pageInNavigation("catalog", false, ["catalog:read"])).toBe(true);
    expect(pageInNavigation("catalog", false, ["cases:read_all"])).toBe(false);
    expect(pageInNavigation("catalog", true, [])).toBe(false);
  });

  it("keeps the admin-only pages out of the navigation for everyone else", () => {
    expect(pageInNavigation("users", false, ["catalog:read"])).toBe(false);
    expect(pageInNavigation("users", true, [])).toBe(true);
    expect(pageInNavigation("overview", false, [])).toBe(true);
  });
});
