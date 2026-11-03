import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { HistoryPage, getCaseHistory } from "../../api";
import { CaseHistory } from "./CaseHistory";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, getCaseHistory: vi.fn() };
});

vi.mock("../../system-timezone", () => ({
  useSystemTimezone: () => ({ timezone: "UTC", effectiveFrom: null, error: null, setCurrent: () => {}, reload: () => {} }),
}));

const history = vi.mocked(getCaseHistory);

function page(overrides: Partial<HistoryPage>): HistoryPage {
  return { items: [], next_cursor: null, ...overrides };
}

beforeEach(() => {
  history.mockReset();
});

describe("CaseHistory", () => {
  it("shows the action, the actor and the details when the caller sees them", async () => {
    history.mockResolvedValue(
      page({
        items: [
          {
            id: "entry-1",
            action: "case.closed",
            created_at: "2026-11-01T06:00:00Z",
            actor_id: "user-1",
            details: { status: { old: "open", new: "closed" } },
          },
        ],
      }),
    );
    render(<CaseHistory caseId="case-1" refreshKey={0} />);

    expect(await screen.findByText("Case closed")).toBeInTheDocument();
    expect(screen.getByText("user-1")).toBeInTheDocument();
    expect(screen.getByText("Status: open -> closed")).toBeInTheDocument();
  });

  it("labels the actor and the details as restricted for a stripped entry", async () => {
    history.mockResolvedValue(
      page({
        items: [{ id: "entry-1", action: "case.create", created_at: "2026-11-01T06:00:00Z", actor_id: null, details: null }],
      }),
    );
    render(<CaseHistory caseId="case-1" refreshKey={0} />);

    expect(await screen.findByText("Case created")).toBeInTheDocument();
    expect(screen.getAllByText("Restricted")).toHaveLength(2);
    expect(screen.queryByText("Not shown")).not.toBeInTheDocument();
    expect(screen.queryByText("None recorded")).not.toBeInTheDocument();
  });

  it("labels only the actor as restricted when the details are visible", async () => {
    history.mockResolvedValue(
      page({
        items: [
          {
            id: "entry-1",
            action: "case.reopened",
            created_at: "2026-11-01T06:00:00Z",
            actor_id: null,
            details: { reason: "New evidence arrived" },
          },
        ],
      }),
    );
    render(<CaseHistory caseId="case-1" refreshKey={0} />);

    expect(await screen.findByText("Case reopened")).toBeInTheDocument();
    expect(screen.getAllByText("Restricted")).toHaveLength(1);
    expect(screen.getByText("Reason: New evidence arrived")).toBeInTheDocument();
  });

  it("shows the close and reopen reasons when the caller has full access", async () => {
    history.mockResolvedValue(
      page({
        items: [
          {
            id: "entry-2",
            action: "case.reopened",
            created_at: "2026-11-01T07:00:00Z",
            actor_id: "user-1",
            details: { reason: "New evidence arrived" },
          },
          {
            id: "entry-1",
            action: "case.closed",
            created_at: "2026-11-01T06:00:00Z",
            actor_id: "user-1",
            details: { reason: "Incident contained" },
          },
        ],
      }),
    );
    render(<CaseHistory caseId="case-1" refreshKey={0} />);

    expect(await screen.findByText("Reason: New evidence arrived")).toBeInTheDocument();
    expect(screen.getByText("Reason: Incident contained")).toBeInTheDocument();
    expect(screen.queryByText("Restricted")).not.toBeInTheDocument();
  });

  it("says so when an entry for a full-access caller has no details", async () => {
    history.mockResolvedValue(
      page({
        items: [{ id: "entry-1", action: "case.assignment_removed", created_at: "2026-11-01T06:00:00Z", actor_id: "user-1", details: {} }],
      }),
    );
    render(<CaseHistory caseId="case-1" refreshKey={0} />);

    expect(await screen.findByText("None recorded")).toBeInTheDocument();
    expect(screen.queryByText("Restricted")).not.toBeInTheDocument();
  });

  it("follows next_cursor forward and back, and stops at the last page", async () => {
    history
      .mockResolvedValueOnce(
        page({
          next_cursor: "cursor-2",
          items: [{ id: "entry-1", action: "case.create", created_at: "2026-11-01T06:00:00Z", actor_id: null, details: null }],
        }),
      )
      .mockResolvedValueOnce(
        page({
          items: [{ id: "entry-2", action: "case.closed", created_at: "2026-11-01T05:00:00Z", actor_id: null, details: null }],
        }),
      )
      .mockResolvedValueOnce(
        page({
          next_cursor: "cursor-2",
          items: [{ id: "entry-1", action: "case.create", created_at: "2026-11-01T06:00:00Z", actor_id: null, details: null }],
        }),
      );
    const user = userEvent.setup();
    render(<CaseHistory caseId="case-1" refreshKey={0} />);

    await screen.findByText("Case created");
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Next" }));

    await waitFor(() => expect(history).toHaveBeenLastCalledWith("case-1", 20, "cursor-2"));
    expect(await screen.findByText("Case closed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Previous" }));
    await waitFor(() => expect(history).toHaveBeenLastCalledWith("case-1", 20, undefined));
    expect(await screen.findByText("Case created")).toBeInTheDocument();
  });

  it("reloads when the refresh key changes", async () => {
    history.mockResolvedValue(page({}));
    const { rerender } = render(<CaseHistory caseId="case-1" refreshKey={0} />);

    await screen.findByText("No history yet.");
    rerender(<CaseHistory caseId="case-1" refreshKey={1} />);

    await waitFor(() => expect(history).toHaveBeenCalledTimes(2));
  });
});
