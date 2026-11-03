import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, PreliminaryReport, getPreliminaryReport } from "../../api";
import { CasePreliminaryReport } from "./CasePreliminaryReport";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, getPreliminaryReport: vi.fn() };
});

vi.mock("../../system-timezone", () => ({
  useSystemTimezone: () => ({ timezone: "UTC", effectiveFrom: null, error: null, setCurrent: () => {}, reload: () => {} }),
}));

const report = vi.mocked(getPreliminaryReport);

const INCOMPLETE: PreliminaryReport = {
  case_id: "case-1",
  reference: "CASE-1",
  case_status: "open",
  generated_at: "2026-11-01T06:00:00Z",
  awareness_time: "2026-11-01T05:00:00Z",
  complete: false,
  missing_fields: [
    { field: "contact", state: "missing" },
    { field: "classification.containment", state: "missing" },
    { field: "impacts.severity", state: "unknown" },
  ],
  contact: { status: "missing" },
  classification: {
    version: 1,
    triggers: ["boundary_escape"],
    event_kind: "incident",
    classification_rationale: "Attacker reached the admin panel",
    responsible_operator: null,
    affected_versions: null,
    approved_scope: null,
    scope_permission_source: null,
    containment: null,
    evidence_gap_plan: null,
    next_update_due_at: null,
  },
  awareness_events: [
    {
      id: "event-1",
      awareness_kind: "operator",
      event_kind: "incident",
      occurred_at: "2026-11-01T05:00:00Z",
      basis: "Alert from the log monitor",
    },
  ],
  impacts: [{ area: "severity", assessed: true, severity: "unknown", scope: "Unknown so far" }],
  legal_duties: [],
  safe_deadlines: [{ kind: "preliminary_safe_report", due_at: "2026-11-02T06:00:00Z", status: "open" }],
};

beforeEach(() => {
  report.mockReset().mockResolvedValue(INCOMPLETE);
});

describe("CasePreliminaryReport", () => {
  it("lists the missing fields and marks the report incomplete", async () => {
    render(<CasePreliminaryReport caseId="case-1" refreshKey={0} />);

    expect(await screen.findByText("Incomplete")).toBeInTheDocument();
    expect(report).toHaveBeenCalledWith("case-1");
    expect(screen.getByText("Reporter contact", { selector: "li" })).toBeInTheDocument();
    expect(screen.getByText("Classification: Containment")).toBeInTheDocument();
    expect(screen.getByText("Impact: Severity (unknown)")).toBeInTheDocument();
  });

  it("shows the contact only as missing or recorded", async () => {
    const { unmount } = render(<CasePreliminaryReport caseId="case-1" refreshKey={0} />);
    expect(await screen.findByText("Missing", { selector: "p" })).toBeInTheDocument();
    unmount();

    report.mockResolvedValue({
      ...INCOMPLETE,
      complete: true,
      missing_fields: [],
      contact: { status: "recorded" },
    });
    render(<CasePreliminaryReport caseId="case-1" refreshKey={0} />);
    expect(await screen.findByText("Recorded", { selector: "p" })).toBeInTheDocument();
    expect(screen.getByText("Complete")).toBeInTheDocument();
    expect(screen.getByText("Nothing is missing.")).toBeInTheDocument();
  });

  it("shows the free text of the record and the awareness events", async () => {
    render(<CasePreliminaryReport caseId="case-1" refreshKey={0} />);

    expect(await screen.findByText("Attacker reached the admin panel")).toBeInTheDocument();
    expect(screen.getByText("Alert from the log monitor")).toBeInTheDocument();
    expect(screen.getByText("Unknown so far")).toBeInTheDocument();
  });

  it("reloads the report when the refresh key changes", async () => {
    const { rerender } = render(<CasePreliminaryReport caseId="case-1" refreshKey={0} />);
    expect(await screen.findByText("Incomplete")).toBeInTheDocument();
    expect(report).toHaveBeenCalledTimes(1);

    rerender(<CasePreliminaryReport caseId="case-1" refreshKey={1} />);

    await waitFor(() => expect(report).toHaveBeenCalledTimes(2));
  });

  it("shows the server message when the report cannot be loaded", async () => {
    report.mockRejectedValue(new ApiError(403, "forbidden"));
    render(<CasePreliminaryReport caseId="case-1" refreshKey={0} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("You do not have permission for this action.");
  });
});
