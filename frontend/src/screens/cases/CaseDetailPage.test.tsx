import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  CaseDetail,
  Classification,
  PreliminaryReport,
  closeCase,
  getCase,
  getCaseHistory,
  getClassification,
  getPreliminaryReport,
  getReporterContact,
  listAssignments,
  listAwarenessEvents,
  listEvidence,
  listImpacts,
  listLegalDuties,
  listReviewRounds,
  openReviewRound,
} from "../../api";
import { CaseDetailPage } from "./CaseDetailPage";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return {
    ...actual,
    getCase: vi.fn(),
    closeCase: vi.fn(),
    openReviewRound: vi.fn(),
    listAssignments: vi.fn(),
    listEvidence: vi.fn(),
    listReviewRounds: vi.fn(),
    getCaseHistory: vi.fn(),
    listAwarenessEvents: vi.fn(),
    listImpacts: vi.fn(),
    listLegalDuties: vi.fn(),
    getClassification: vi.fn(),
    getPreliminaryReport: vi.fn(),
    getReporterContact: vi.fn(),
  };
});

vi.mock("../../system-timezone", () => ({
  useSystemTimezone: () => ({ timezone: "UTC", effectiveFrom: null, error: null, setCurrent: () => {}, reload: () => {} }),
}));

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const get = vi.mocked(getCase);
const close = vi.mocked(closeCase);
const open = vi.mocked(openReviewRound);
const assignments = vi.mocked(listAssignments);
const evidence = vi.mocked(listEvidence);
const reviews = vi.mocked(listReviewRounds);
const history = vi.mocked(getCaseHistory);
const awarenessEvents = vi.mocked(listAwarenessEvents);
const impacts = vi.mocked(listImpacts);
const legalDuties = vi.mocked(listLegalDuties);
const classification = vi.mocked(getClassification);
const report = vi.mocked(getPreliminaryReport);
const read = vi.mocked(getReporterContact);

const NO_CLASSIFICATION: Classification = {
  case_id: "case-1",
  version: 0,
  triggers: [],
  event_kind: null,
  classification_rationale: null,
  responsible_operator: null,
  affected_versions: null,
  approved_scope: null,
  scope_permission_source: null,
  containment: null,
  evidence_gap_plan: null,
  next_update_due_at: null,
  created_by: null,
  created_at: null,
};

const EMPTY_REPORT: PreliminaryReport = {
  case_id: "case-1",
  reference: "CASE-1",
  case_status: "open",
  generated_at: "2026-11-01T06:00:00Z",
  awareness_time: "2026-11-01T05:00:00Z",
  complete: false,
  missing_fields: [{ field: "contact", state: "missing" }],
  contact: { status: "missing" },
  classification: {
    version: 0,
    triggers: [],
    event_kind: null,
    classification_rationale: null,
    responsible_operator: null,
    affected_versions: null,
    approved_scope: null,
    scope_permission_source: null,
    containment: null,
    evidence_gap_plan: null,
    next_update_due_at: null,
  },
  awareness_events: [],
  impacts: [],
  legal_duties: [],
  safe_deadlines: [],
};

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
  risk_score: null,
  impact_c: null,
  impact_b: null,
  impact_a: null,
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
  open.mockReset();
  assignments.mockReset().mockResolvedValue([]);
  evidence.mockReset().mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0 });
  reviews.mockReset().mockResolvedValue([]);
  history.mockReset().mockResolvedValue({ items: [], total: 0, limit: 20, offset: 0 });
  awarenessEvents.mockReset().mockResolvedValue([]);
  impacts.mockReset().mockResolvedValue([]);
  legalDuties.mockReset().mockResolvedValue([]);
  classification.mockReset().mockResolvedValue(NO_CLASSIFICATION);
  report.mockReset().mockResolvedValue(EMPTY_REPORT);
  read.mockReset();
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
    expect(await screen.findByRole("button", { name: "Open review round" })).toBeInTheDocument();
  });

  it("lists the review rounds but hides the open button without reviews:write", async () => {
    render(<CaseDetailPage caseId="case-1" permissions={FULL_ACCESS} />);

    expect(await screen.findByText("Review", { exact: true })).toBeInTheDocument();
    expect(await screen.findByText("No review round has been opened for this case yet.")).toBeInTheDocument();
    expect(reviews).toHaveBeenCalledWith("case-1");
    expect(screen.queryByRole("button", { name: "Open review round" })).not.toBeInTheDocument();
  });

  it("shows the review rounds of the case to a reporter who can read it", async () => {
    render(<CaseDetailPage caseId="case-1" permissions={REPORTER} />);

    expect(await screen.findByText("Review", { exact: true })).toBeInTheDocument();
    expect(await screen.findByText("No review round has been opened for this case yet.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open review round" })).not.toBeInTheDocument();
  });

  it("reloads the history after a review round is opened", async () => {
    open.mockResolvedValue({
      id: "round-1",
      case_id: "case-1",
      status: "open",
      revision: 1,
      opened_at: "2026-11-01T09:00:00Z",
      closed_at: null,
      layers: [],
    });
    const user = userEvent.setup();
    render(<CaseDetailPage caseId="case-1" permissions={REVIEWER} />);

    await screen.findByText("Example incident");
    expect(history).toHaveBeenCalledTimes(1);

    await user.click(await screen.findByRole("button", { name: "Open review round" }));

    await waitFor(() => expect(history).toHaveBeenCalledTimes(2));
    expect(open).toHaveBeenCalledWith("case-1");
  });

  it("shows the case record sections to a reporter who can read the case, without the report", async () => {
    render(<CaseDetailPage caseId="case-1" permissions={REPORTER} />);

    expect(await screen.findByText("Example incident")).toBeInTheDocument();
    expect(awarenessEvents).toHaveBeenCalledWith("case-1");
    expect(impacts).toHaveBeenCalledWith("case-1");
    expect(legalDuties).toHaveBeenCalledWith("case-1");
    expect(classification).toHaveBeenCalledWith("case-1");
    expect(screen.queryByText("Preliminary report")).not.toBeInTheDocument();
    expect(report).not.toHaveBeenCalled();
  });

  it("shows the preliminary report only to a caller with cases:read_all", async () => {
    render(<CaseDetailPage caseId="case-1" permissions={FULL_ACCESS} />);

    expect(await screen.findByText("Preliminary report")).toBeInTheDocument();
    expect(report).toHaveBeenCalledWith("case-1");
  });

  it("offers no case record write control to a read-only caller", async () => {
    render(<CaseDetailPage caseId="case-1" permissions={["cases:read_all"]} />);

    expect(await screen.findByText("Example incident")).toBeInTheDocument();
    expect(await screen.findByText("No awareness event has been recorded for this case yet.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add awareness event" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add legal duty" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit classification" })).not.toBeInTheDocument();
  });

  it("hides the case record write controls once the case is closed", async () => {
    get.mockResolvedValue({ ...OPEN_CASE, status: "closed", closed_at: "2026-11-01T07:00:00Z", close_reason: "Done" });
    render(<CaseDetailPage caseId="case-1" permissions={FULL_ACCESS} />);

    expect(await screen.findByText("Closed")).toBeInTheDocument();
    expect(await screen.findByText("No awareness event has been recorded for this case yet.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add awareness event" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add legal duty" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit classification" })).not.toBeInTheDocument();
  });

  it("offers the reporter contact to a caller with reporter_contact:read, without reading it on load", async () => {
    render(<CaseDetailPage caseId="case-1" permissions={["cases:read_all", "reporter_contact:read"]} />);

    expect(await screen.findByRole("button", { name: "Show reporter contact" })).toBeInTheDocument();
    expect(read).not.toHaveBeenCalled();
  });

  it("hides the reporter contact without reporter_contact:read", async () => {
    render(<CaseDetailPage caseId="case-1" permissions={FULL_ACCESS} />);

    await screen.findByText("Example incident");
    expect(screen.queryByRole("button", { name: "Show reporter contact" })).not.toBeInTheDocument();
    expect(read).not.toHaveBeenCalled();
  });
});
