import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { listCases } from "../../api";
import { formatInTimezone } from "@/time";
import { CasesPage } from "./CasesPage";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, listCases: vi.fn() };
});

vi.mock("../../system-timezone", () => ({
  useSystemTimezone: () => ({ timezone: "UTC", effectiveFrom: null, error: null, setCurrent: () => {}, reload: () => {} }),
}));

const list = vi.mocked(listCases);

// A due date in the past is overdue, one far in the future is not. UPDATED differs from every other date in the rows.
const FUTURE = "2099-01-15T09:30:00Z";
const PAST = "2001-01-15T09:30:00Z";
const UPDATED = "2026-11-01T07:45:00Z";
const BASE_ITEM = {
  reference: "SAFE-2026-000001",
  title: "Phishing",
  awareness_time: "2026-10-30T09:00:00Z",
  status: "open",
  created_at: "2026-10-31T10:00:00Z",
  next_deadline: null,
  updated_at: UPDATED,
  risk_score: null,
  impact_c: null,
  impact_b: null,
  impact_a: null,
};

beforeEach(() => {
  list.mockReset().mockResolvedValue({ items: [], next_cursor: null });
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
    const item = { ...BASE_ITEM, reference: "SAFE-2026-000001", title: "Phishing", awareness_time: "2026-10-30T09:00:00Z", created_at: "2026-10-31T10:00:00Z" };
    list.mockResolvedValue({
      items: [
        { id: "case-1", ...item, risk_score: 3.7, impact_c: 4, impact_b: 3, impact_a: 4 },
        { id: "case-2", ...item, reference: "SAFE-2026-000002", risk_score: null, impact_c: null, impact_b: null, impact_a: null },
      ],
      next_cursor: null,
    });
    render(<CasesPage permissions={["cases:read_all"]} />);

    const table = await screen.findByRole("table");
    expect(within(table).getByRole("columnheader", { name: "Risk score" })).toBeInTheDocument();
    const rows = within(table).getAllByRole("row").slice(1);
    expect(within(rows[0]).getByText("3.7 (High)")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Not assessed")).toBeInTheDocument();
  });

  it("keeps the existing columns and adds Next deadline and Last updated", async () => {
    list.mockResolvedValue({ items: [{ ...BASE_ITEM, id: "case-1" }], next_cursor: null });
    render(<CasesPage permissions={["cases:read_all"]} />);

    const table = await screen.findByRole("table");
    for (const name of ["Reference", "Title", "Status", "Risk score", "Awareness time", "Next deadline", "Last updated", "Created"]) {
      expect(within(table).getByRole("columnheader", { name })).toBeInTheDocument();
    }
  });

  it("shows the next deadline with its date and time, None when no task is open, and Overdue when it has passed", async () => {
    list.mockResolvedValue({
      items: [
        { ...BASE_ITEM, id: "case-1", reference: "SAFE-2026-000001", next_deadline: FUTURE },
        { ...BASE_ITEM, id: "case-2", reference: "SAFE-2026-000002", next_deadline: PAST },
        { ...BASE_ITEM, id: "case-3", reference: "SAFE-2026-000003", next_deadline: null },
      ],
      next_cursor: null,
    });
    render(<CasesPage permissions={["cases:read_all"]} />);

    const table = await screen.findByRole("table");
    const rows = within(table).getAllByRole("row").slice(1);
    expect(within(rows[0]).getByText(formatInTimezone(FUTURE, "UTC"))).toBeInTheDocument();
    expect(within(rows[0]).queryByText("Overdue")).not.toBeInTheDocument();
    expect(within(rows[1]).getByText(formatInTimezone(PAST, "UTC"))).toBeInTheDocument();
    expect(within(rows[1]).getByText("Overdue")).toBeInTheDocument();
    expect(within(rows[2]).getByText("None")).toBeInTheDocument();
    expect(within(rows[2]).queryByText("Overdue")).not.toBeInTheDocument();
  });

  it("shows the last update of each case", async () => {
    list.mockResolvedValue({ items: [{ ...BASE_ITEM, id: "case-1" }], next_cursor: null });
    render(<CasesPage permissions={["cases:read_all"]} />);

    const table = await screen.findByRole("table");
    const [row] = within(table).getAllByRole("row").slice(1);
    expect(within(row).getByText(formatInTimezone(UPDATED, "UTC"))).toBeInTheDocument();
  });

  it("filters by status: All statuses first, then the chosen status is sent and the list starts again", async () => {
    const user = userEvent.setup();
    render(<CasesPage permissions={["cases:read_all"]} />);

    await screen.findByText("You have not opened any cases yet.");
    const filter = screen.getByRole("combobox", { name: "Filter by status" });
    expect(filter).toHaveTextContent("All statuses");
    expect(list).toHaveBeenLastCalledWith(20, undefined, "", undefined);

    await user.click(filter);
    await user.click(await screen.findByRole("option", { name: "Closed" }));

    await waitFor(() => expect(list).toHaveBeenLastCalledWith(20, undefined, "", "closed"));
    expect(screen.getByRole("combobox", { name: "Filter by status" })).toHaveTextContent("Closed");
    expect(await screen.findByText("No cases have this status.")).toBeInTheDocument();
  });

  it("sends the search to the server and says when nothing matches", async () => {
    const user = userEvent.setup();
    render(<CasesPage permissions={["cases:read_all"]} />);

    await screen.findByText("You have not opened any cases yet.");
    await user.type(screen.getByRole("searchbox", { name: "Search cases" }), "  SAFE-2026 ");

    await waitFor(() => expect(list).toHaveBeenLastCalledWith(20, undefined, "SAFE-2026", undefined));
    expect(await screen.findByText("No cases match this search.")).toBeInTheDocument();
  });

  it("pages forward with next_cursor and back with Previous, and disables the buttons with nothing to go to", async () => {
    const user = userEvent.setup();
    list
      .mockResolvedValueOnce({ items: [{ ...BASE_ITEM, id: "case-1", reference: "SAFE-2026-000001" }], next_cursor: "cursor-2" })
      .mockResolvedValueOnce({ items: [{ ...BASE_ITEM, id: "case-2", reference: "SAFE-2026-000002" }], next_cursor: null })
      .mockResolvedValue({ items: [{ ...BASE_ITEM, id: "case-1", reference: "SAFE-2026-000001" }], next_cursor: "cursor-2" });
    render(<CasesPage permissions={["cases:read_all"]} />);

    expect(await screen.findByRole("link", { name: "SAFE-2026-000001" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    await waitFor(() => expect(screen.getByRole("button", { name: "Next" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Next" }));

    expect(await screen.findByRole("link", { name: "SAFE-2026-000002" })).toBeInTheDocument();
    expect(list).toHaveBeenLastCalledWith(20, "cursor-2", "", undefined);
    await waitFor(() => expect(screen.getByRole("button", { name: "Previous" })).toBeEnabled());
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Previous" }));

    expect(await screen.findByRole("link", { name: "SAFE-2026-000001" })).toBeInTheDocument();
    expect(list).toHaveBeenLastCalledWith(20, undefined, "", undefined);
    await waitFor(() => expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled());
  });

  it("starts again at the first page when the search or the status filter changes", async () => {
    const user = userEvent.setup();
    list.mockResolvedValue({ items: [{ ...BASE_ITEM, id: "case-1" }], next_cursor: "cursor-2" });
    render(<CasesPage permissions={["cases:read_all"]} />);

    await waitFor(() => expect(screen.getByRole("button", { name: "Next" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(list).toHaveBeenLastCalledWith(20, "cursor-2", "", undefined));

    await user.click(screen.getByRole("combobox", { name: "Filter by status" }));
    await user.click(await screen.findByRole("option", { name: "Closed" }));
    await waitFor(() => expect(list).toHaveBeenLastCalledWith(20, undefined, "", "closed"));
    await waitFor(() => expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled());

    await waitFor(() => expect(screen.getByRole("button", { name: "Next" })).toBeEnabled());
    await user.click(screen.getByRole("button", { name: "Next" }));
    await waitFor(() => expect(list).toHaveBeenLastCalledWith(20, "cursor-2", "", "closed"));

    await user.type(screen.getByRole("searchbox", { name: "Search cases" }), "SAFE");
    await waitFor(() => expect(list).toHaveBeenLastCalledWith(20, undefined, "SAFE", "closed"));
    await waitFor(() => expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled());
  });

  // --- empty and error states: an empty or failed list is never shown as a clean result --------------------------------

  it("never reads an empty list as a pass, a clean result or a compliance result", async () => {
    render(<CasesPage permissions={["cases:read_all"]} />);

    expect(await screen.findByText("You have not opened any cases yet.")).toBeInTheDocument();
    expect(screen.queryByText(/\bpass(ed)?\b|\bclean\b|compliant|compliance|verified/i)).not.toBeInTheDocument();
  });

  it("says that no case has the chosen status when a filter matches nothing", async () => {
    const user = userEvent.setup();
    render(<CasesPage permissions={["cases:read_all"]} />);

    await screen.findByText("You have not opened any cases yet.");
    await user.click(screen.getByRole("combobox", { name: "Filter by status" }));
    await user.click(await screen.findByRole("option", { name: "Closed" }));

    expect(await screen.findByText("No cases have this status.")).toBeInTheDocument();
  });

  it("shows the refusal and no empty or loading text when the first list cannot be loaded", async () => {
    const { ApiError } = await vi.importActual<typeof import("../../api")>("../../api");
    list.mockRejectedValue(new ApiError(403, "forbidden"));
    render(<CasesPage permissions={["cases:read_all"]} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("You do not have permission for this action.");
    expect(screen.queryByText("You have not opened any cases yet.")).not.toBeInTheDocument();
    expect(screen.queryByText("No cases match this search.")).not.toBeInTheDocument();
    expect(screen.queryByText("Loading cases…")).not.toBeInTheDocument();
  });

  it("does not keep the rows of an earlier filter under an error for the new filter", async () => {
    const user = userEvent.setup();
    const { ApiError } = await vi.importActual<typeof import("../../api")>("../../api");
    list.mockResolvedValue({ items: [{ ...BASE_ITEM, id: "case-1" }], next_cursor: null });
    render(<CasesPage permissions={["cases:read_all"]} />);
    expect(await screen.findByRole("link", { name: "SAFE-2026-000001" })).toBeInTheDocument();

    list.mockRejectedValue(new ApiError(500, "internal_error"));
    await user.click(screen.getByRole("combobox", { name: "Filter by status" }));
    await user.click(await screen.findByRole("option", { name: "Closed" }));

    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "SAFE-2026-000001" })).not.toBeInTheDocument();
    expect(screen.queryByText("No cases have this status.")).not.toBeInTheDocument();
    expect(screen.queryByText("No cases match this search.")).not.toBeInTheDocument();
  });
});
