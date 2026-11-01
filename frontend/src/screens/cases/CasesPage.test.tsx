import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { listCases } from "../../api";
import { CasesPage } from "./CasesPage";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, listCases: vi.fn() };
});

vi.mock("../../system-timezone", () => ({
  useSystemTimezone: () => ({ timezone: "UTC", effectiveFrom: null, error: null, setCurrent: () => {}, reload: () => {} }),
}));

const list = vi.mocked(listCases);

beforeEach(() => {
  list.mockReset().mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0 });
});

describe("CasesPage", () => {
  it("offers New case to a caller with cases:submit", async () => {
    render(<CasesPage permissions={["cases:submit", "cases:read_own"]} />);

    expect(await screen.findByRole("button", { name: "New case" })).toBeInTheDocument();
  });

  it("hides New case from a caller without cases:submit", async () => {
    render(<CasesPage permissions={["cases:read_all", "cases:triage", "cases:close"]} />);

    expect(await screen.findByText("You have not opened any cases yet.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "New case" })).not.toBeInTheDocument();
  });
});
