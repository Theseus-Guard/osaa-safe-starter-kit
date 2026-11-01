import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { listCases, Me } from "../api";
import { AppShell } from "./AppShell";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, listCases: vi.fn() };
});

vi.mock("../system-timezone", () => ({
  useSystemTimezone: () => ({ timezone: "UTC", effectiveFrom: null, error: null, setCurrent: () => {}, reload: () => {} }),
}));

function profileWith(permissions: string[]): Me {
  return {
    id: "user-1",
    username: "example-user",
    role: "reporter",
    first_name: null,
    last_name: null,
    title: null,
    email: "user@example.com",
    password_change_required: false,
    timezone_setup_required: false,
    permissions,
  };
}

function renderShell(permissions: string[]) {
  return render(
    <AppShell profile={profileWith(permissions)} page="overview" onLogout={() => {}}>
      <p>Page body</p>
    </AppShell>,
  );
}

describe("AppShell case search", () => {
  it.each(["cases:read_own", "cases:read_all", "cases:read_assigned"])("shows the search to a caller with %s", (permission) => {
    renderShell([permission]);

    expect(screen.getByRole("searchbox", { name: "Search cases by ID" })).toBeInTheDocument();
    expect(screen.getByPlaceholderText("Search cases by ID")).toBeInTheDocument();
  });

  it("hides the search from a caller without a case-read permission", () => {
    renderShell(["cases:submit", "cases:close", "users:manage"]);

    expect(screen.queryByRole("searchbox", { name: "Search cases by ID" })).not.toBeInTheDocument();
    expect(screen.getByText("Page body")).toBeInTheDocument();
  });

  it("does not query the backend when the header renders", () => {
    renderShell(["cases:read_own"]);

    expect(vi.mocked(listCases)).not.toHaveBeenCalled();
  });
});
