import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
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

  it("shows a Risk score column with the score of each case, or Not assessed", async () => {
    const item = { reference: "SAFE-2026-000001", title: "Phishing", awareness_time: "2026-10-30T09:00:00Z", status: "open", created_at: "2026-10-31T10:00:00Z" };
    list.mockResolvedValue({
      items: [
        { id: "case-1", ...item, risk_score: 3.7, impact_c: 4, impact_b: 3, impact_a: 4 },
        { id: "case-2", ...item, reference: "SAFE-2026-000002", risk_score: null, impact_c: null, impact_b: null, impact_a: null },
      ],
      total: 2,
      limit: 20,
      offset: 0,
    });
    render(<CasesPage permissions={["cases:read_all"]} />);

    const table = await screen.findByRole("table");
    expect(within(table).getByRole("columnheader", { name: "Risk score" })).toBeInTheDocument();
    const rows = within(table).getAllByRole("row").slice(1);
    expect(within(rows[0]).getByText("3.7")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Not assessed")).toBeInTheDocument();
  });

  it("sends the search to the server and says when nothing matches", async () => {
    const user = userEvent.setup();
    render(<CasesPage permissions={["cases:read_all"]} />);

    await screen.findByText("You have not opened any cases yet.");
    await user.type(screen.getByRole("searchbox", { name: "Search cases" }), "  SAFE-2026 ");

    await waitFor(() => expect(list).toHaveBeenLastCalledWith(20, 0, "SAFE-2026"));
    expect(await screen.findByText("No cases match this search.")).toBeInTheDocument();
  });
});
