import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CaseDetail, closeCase, createReview, getCase, getCaseHistory, listAssignments, listEvidence } from "../../api";
import { CaseDetailPage } from "./CaseDetailPage";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return {
    ...actual,
    getCase: vi.fn(),
    closeCase: vi.fn(),
    createReview: vi.fn(),
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
const review = vi.mocked(createReview);
const assignments = vi.mocked(listAssignments);
const evidence = vi.mocked(listEvidence);
const history = vi.mocked(getCaseHistory);

// A triager with full case access and evidence access.
const FULL_ACCESS = [
  "cases:read_all",
  "cases:triage",
  "cases:assign",
  "cases:close",
  "evidence:read",
  "evidence:write",
];
// A reporter can submit cases and see their own. The case controls stay locked and evidence is hidden.
const REPORTER = ["cases:submit", "cases:read_own"];
// A reviewer records reviews on the cases assigned to them.
const REVIEWER = ["cases:read_assigned", "evidence:read", "reviews:write"];

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
  review.mockReset();
  assignments.mockReset().mockResolvedValue([]);
  evidence.mockReset().mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0 });
  history.mockReset().mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0 });
});

describe("CaseDetailPage", () => {
  it("shows the case with its deadlines, assignments, evidence and history", async () => {
    render(<CaseDetailPage caseId="case-1" permissions={FULL_ACCESS} />);

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
    render(<CaseDetailPage caseId="case-1" permissions={FULL_ACCESS} />);

    await screen.findByText("Example incident");
    expect(history).toHaveBeenCalledTimes(1);

    await user.type(await screen.findByLabelText("Reason"), "Done");
    await user.click(screen.getByRole("button", { name: "Close case" }));

    await waitFor(() => expect(history).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("Closed")).toBeInTheDocument();
  });

  it("hides the evidence section without evidence:read or evidence:write", async () => {
    render(<CaseDetailPage caseId="case-1" permissions={REPORTER} />);

    expect(await screen.findByText("Example incident")).toBeInTheDocument();
    expect(screen.queryByText("Evidence", { exact: true })).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Upload evidence/)).not.toBeInTheDocument();
    expect(evidence).not.toHaveBeenCalled();
  });

  it("shows the evidence list without the upload form when the caller can only read", async () => {
    render(<CaseDetailPage caseId="case-1" permissions={["cases:read_all", "evidence:read"]} />);

    expect(await screen.findByText("Evidence", { exact: true })).toBeInTheDocument();
    expect(evidence).toHaveBeenCalledWith("case-1", 20, 0);
    expect(screen.queryByLabelText(/Upload evidence/)).not.toBeInTheDocument();
  });

  it("shows the upload form without loading the list when the caller can only write", async () => {
    render(<CaseDetailPage caseId="case-1" permissions={["cases:read_assigned", "evidence:write"]} />);

    expect(await screen.findByLabelText(/Upload evidence/)).toBeInTheDocument();
    expect(evidence).not.toHaveBeenCalled();
  });

  it("locks the close form for a reporter and offers no reopen", async () => {
    render(<CaseDetailPage caseId="case-1" permissions={REPORTER} />);

    expect(await screen.findByText("Your account cannot close cases.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Close case" })).not.toBeInTheDocument();
  });

  it("offers reopen on a closed case to a caller with cases:close", async () => {
    get.mockResolvedValue({ ...OPEN_CASE, status: "closed", closed_at: "2026-11-01T07:00:00Z", close_reason: "Done" });
    render(<CaseDetailPage caseId="case-1" permissions={FULL_ACCESS} />);

    expect(await screen.findByRole("button", { name: "Reopen case" })).toBeInTheDocument();
  });

  it("hides reopen on a closed case from a caller without cases:close", async () => {
    get.mockResolvedValue({ ...OPEN_CASE, status: "closed", closed_at: "2026-11-01T07:00:00Z", close_reason: "Done" });
    render(<CaseDetailPage caseId="case-1" permissions={["cases:read_all", "evidence:read"]} />);

    expect(await screen.findByText("Closed")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reopen case" })).not.toBeInTheDocument();
  });

  it("shows the reopen reason from the case detail", async () => {
    get.mockResolvedValue({
      ...OPEN_CASE,
      status: "open",
      reopen_reason: "New evidence arrived",
    });
    render(<CaseDetailPage caseId="case-1" permissions={FULL_ACCESS} />);

    expect(await screen.findByText("New evidence arrived")).toBeInTheDocument();
  });

  it("shows the review section to a caller with reviews:write", async () => {
    render(<CaseDetailPage caseId="case-1" permissions={REVIEWER} />);

    expect(await screen.findByText("Review", { exact: true })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Record review" })).toBeInTheDocument();
  });

  it("hides the review section without reviews:write", async () => {
    render(<CaseDetailPage caseId="case-1" permissions={FULL_ACCESS} />);

    expect(await screen.findByText("Example incident")).toBeInTheDocument();
    expect(screen.queryByText("Review", { exact: true })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Record review" })).not.toBeInTheDocument();
  });

  it("reloads the history after a review is recorded", async () => {
    review.mockResolvedValue({ id: "review-1", case_id: "case-1", outcome: "pass", note: null });
    const user = userEvent.setup();
    render(<CaseDetailPage caseId="case-1" permissions={REVIEWER} />);

    await screen.findByText("Example incident");
    expect(history).toHaveBeenCalledTimes(1);

    await user.click(await screen.findByRole("button", { name: "Record review" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByLabelText("Outcome"));
    await user.click(await screen.findByRole("option", { name: "Pass" }));
    await user.click(within(dialog).getByRole("button", { name: "Submit review" }));

    await waitFor(() => expect(history).toHaveBeenCalledTimes(2));
    expect(review).toHaveBeenCalledWith("case-1", { outcome: "pass", note: null });
  });
});
