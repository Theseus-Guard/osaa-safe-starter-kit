import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { ImpactAssessment, listImpacts, setImpact } from "../../api";
import { CaseImpacts } from "./CaseImpacts";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, listImpacts: vi.fn(), setImpact: vi.fn() };
});

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const list = vi.mocked(listImpacts);
const save = vi.mocked(setImpact);

// The server lists every area. "scale" is assessed as none; "duration" has no assessment yet.
const IMPACTS: ImpactAssessment[] = [
  { area: "affected_people_groups", assessed: false, severity: null, scope: null, updated_at: null },
  { area: "severity", assessed: true, severity: "high", scope: "Customer records", updated_at: "2026-11-01T06:00:00Z" },
  { area: "scale", assessed: true, severity: "none", scope: "One site", updated_at: "2026-11-01T06:00:00Z" },
  { area: "duration", assessed: false, severity: null, scope: null, updated_at: null },
  { area: "reversibility", assessed: false, severity: null, scope: null, updated_at: null },
  { area: "disproportionate_effect", assessed: false, severity: null, scope: null, updated_at: null },
];

const BASE_PROPS = {
  caseId: "case-1",
  closed: false,
  canWrite: true,
  canReadFullView: true,
  onRecorded: () => {},
};

function rowOf(area: string): HTMLElement {
  return screen.getByText(area).closest("tr") as HTMLElement;
}

beforeEach(() => {
  list.mockReset().mockResolvedValue(IMPACTS);
  save.mockReset();
  vi.mocked(toast.success).mockClear();
});

describe("CaseImpacts", () => {
  it("lists all six areas and shows an unassessed area as not assessed, not as none", async () => {
    render(<CaseImpacts {...BASE_PROPS} />);

    expect(await screen.findByText("Affected people groups")).toBeInTheDocument();
    expect(screen.getAllByRole("row")).toHaveLength(7);
    expect(rowOf("Scale")).toHaveTextContent("None");
    expect(rowOf("Scale")).not.toHaveTextContent("Not assessed");
    expect(rowOf("Duration")).toHaveTextContent("Not assessed");
    expect(rowOf("Severity")).toHaveTextContent("High");
  });

  it("shows the scope as not shown for a caller without cases:read_all", async () => {
    list.mockResolvedValue(IMPACTS.map((row) => (row.assessed ? { ...row, scope: null } : row)));
    render(<CaseImpacts {...BASE_PROPS} canReadFullView={false} />);

    expect(await screen.findByText("Affected people groups")).toBeInTheDocument();
    expect(rowOf("Severity")).toHaveTextContent("Not shown for your role.");
    expect(rowOf("Duration")).not.toHaveTextContent("Not shown for your role.");
  });

  it("offers no assess control without cases:triage or on a closed case", async () => {
    const { rerender } = render(<CaseImpacts {...BASE_PROPS} canWrite={false} />);
    expect(await screen.findByText("Affected people groups")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Assess|Update/ })).not.toBeInTheDocument();

    rerender(<CaseImpacts {...BASE_PROPS} closed />);
    expect(screen.queryByRole("button", { name: /Assess|Update/ })).not.toBeInTheDocument();
    expect(screen.getByText("This case is closed, so its impact assessment cannot be changed.")).toBeInTheDocument();
  });

  it("saves an assessment of an unassessed area and shows the stored row", async () => {
    save.mockResolvedValue({
      area: "duration",
      assessed: true,
      severity: "medium",
      scope: "Two days",
      updated_at: "2026-11-01T07:00:00Z",
    });
    const onRecorded = vi.fn();
    const user = userEvent.setup();
    render(<CaseImpacts {...BASE_PROPS} onRecorded={onRecorded} />);

    expect(await screen.findByText("Affected people groups")).toBeInTheDocument();
    await user.click(within(rowOf("Duration")).getByRole("button", { name: "Assess" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByLabelText("Severity"));
    await user.click(await screen.findByRole("option", { name: "Medium" }));
    await user.type(within(dialog).getByLabelText("Scope"), "  Two days  ");
    await user.click(within(dialog).getByRole("button", { name: "Save assessment" }));

    await waitFor(() => expect(save).toHaveBeenCalledWith("case-1", "duration", { severity: "medium", scope: "Two days" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(rowOf("Duration")).toHaveTextContent("Medium");
    expect(rowOf("Duration")).toHaveTextContent("Two days");
    expect(onRecorded).toHaveBeenCalledTimes(1);
    expect(toast.success).toHaveBeenCalledWith("Duration assessment saved.");
  });

  it("asks for a severity before saving", async () => {
    const user = userEvent.setup();
    render(<CaseImpacts {...BASE_PROPS} />);

    expect(await screen.findByText("Affected people groups")).toBeInTheDocument();
    await user.click(within(rowOf("Duration")).getByRole("button", { name: "Assess" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText("Scope"), "Two days");
    await user.click(within(dialog).getByRole("button", { name: "Save assessment" }));

    expect(save).not.toHaveBeenCalled();
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Choose the severity for this area.");
  });
});
