import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CaseDetail, closeCase, getCase, getCaseHistory, listAssignments, listEvidence } from "../../api";
import { CaseDetailPage } from "./CaseDetailPage";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return {
    ...actual,
    getCase: vi.fn(),
    closeCase: vi.fn(),
    listAssignments: vi.fn(),
    listEvidence: vi.fn(),
    getCaseHistory: vi.fn(),
  };
});

vi.mock("../../system-timezone", () => ({
  useSystemTimezone: () => ({ timezone: "UTC", effectiveFrom: null, error: null, setCurrent: () => {}, reload: () => {} }),
}));

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const get = vi.mocked(getCase);
const close = vi.mocked(closeCase);
const assignments = vi.mocked(listAssignments);
const evidence = vi.mocked(listEvidence);
const history = vi.mocked(getCaseHistory);

const OPEN_CASE: CaseDetail = {
  id: "case-1",
  reference: "CASE-1",
  title: "Example incident",
  awareness_time: "2026-11-01T05:00:00Z",
  status: "open",
  calendar_version: "2026.1",
  created_at: "2026-11-01T06:00:00Z",
  closed_at: null,
  close_reason: null,
  deadlines: [
    {
      id: "task-1",
      kind: "preliminary_safe_report",
      due_at: "2026-11-02T05:00:00Z",
      status: "open",
      not_applicable_reason: null,
    },
  ],
};

beforeEach(() => {
  get.mockReset().mockResolvedValue(OPEN_CASE);
  close.mockReset();
  assignments.mockReset().mockResolvedValue([]);
  evidence.mockReset().mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0 });
  history.mockReset().mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0 });
});

describe("CaseDetailPage", () => {
  it("shows the case with its deadlines, assignments, evidence and history", async () => {
    render(<CaseDetailPage caseId="case-1" />);

    expect(await screen.findByText("Example incident")).toBeInTheDocument();
    expect(screen.getByText("Deadline tasks")).toBeInTheDocument();
    expect(screen.getByText("Assignments")).toBeInTheDocument();
    expect(screen.getByText("Evidence")).toBeInTheDocument();
    expect(screen.getByText("History")).toBeInTheDocument();
    expect(assignments).toHaveBeenCalledWith("case-1");
    expect(evidence).toHaveBeenCalledWith("case-1", 20, 0);
  });

  it("reloads the history after the case is closed", async () => {
    close.mockResolvedValue({ ...OPEN_CASE, status: "closed", closed_at: "2026-11-01T07:00:00Z", close_reason: "Done" });
    const user = userEvent.setup();
    render(<CaseDetailPage caseId="case-1" />);

    await screen.findByText("Example incident");
    expect(history).toHaveBeenCalledTimes(1);

    await user.type(await screen.findByLabelText("Reason"), "Done");
    await user.click(screen.getByRole("button", { name: "Close case" }));

    await waitFor(() => expect(history).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("Closed")).toBeInTheDocument();
  });
});
