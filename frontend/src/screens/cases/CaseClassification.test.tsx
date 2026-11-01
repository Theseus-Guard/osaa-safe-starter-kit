import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { ApiError, Classification, getClassification, updateClassification } from "../../api";
import { CaseClassification } from "./CaseClassification";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, getClassification: vi.fn(), updateClassification: vi.fn() };
});

vi.mock("../../system-timezone", () => ({
  useSystemTimezone: () => ({ timezone: "UTC", effectiveFrom: null, error: null, setCurrent: () => {}, reload: () => {} }),
}));

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const get = vi.mocked(getClassification);
const save = vi.mocked(updateClassification);

const NONE: Classification = {
  case_id: "case-1",
  version: 0,
  triggers: [],
  event_kind: null,
  internal_incident_option: false,
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

const VERSION_TWO: Classification = {
  ...NONE,
  version: 2,
  triggers: ["boundary_escape", "harm_inaction_near_miss"],
  event_kind: "near_miss",
  internal_incident_option: true,
  classification_rationale: "Harm was avoided by the block",
  containment: null,
  created_by: "user-1",
  created_at: "2026-11-01T06:00:00Z",
};

const BASE_PROPS = {
  caseId: "case-1",
  closed: false,
  canWrite: true,
  canReadFullView: true,
  onRecorded: () => {},
};

beforeEach(() => {
  get.mockReset().mockResolvedValue(NONE);
  save.mockReset();
  vi.mocked(toast.success).mockClear();
});

async function openEditor(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Edit classification" }));
  return screen.findByRole("dialog");
}

describe("CaseClassification", () => {
  it("shows that no classification is recorded yet at version 0", async () => {
    render(<CaseClassification {...BASE_PROPS} />);

    expect(await screen.findByText("No classification is recorded for this case yet.")).toBeInTheDocument();
    expect(get).toHaveBeenCalledWith("case-1");
  });

  it("shows the current version with its triggers and the recorded text", async () => {
    get.mockResolvedValue(VERSION_TWO);
    render(<CaseClassification {...BASE_PROPS} />);

    expect(await screen.findByText("Version 2")).toBeInTheDocument();
    expect(screen.getByText("Boundary escape")).toBeInTheDocument();
    expect(screen.getByText("Harm, inaction or near miss")).toBeInTheDocument();
    expect(screen.getByText("Harm was avoided by the block")).toBeInTheDocument();
    expect(screen.getByText("Yes")).toBeInTheDocument();
    expect(screen.getAllByText("Not recorded.").length).toBeGreaterThan(0);
  });

  it("shows the text as not shown for a caller without cases:read_all, and offers no edit", async () => {
    get.mockResolvedValue({ ...VERSION_TWO, classification_rationale: null });
    render(<CaseClassification {...BASE_PROPS} canReadFullView={false} />);

    expect(await screen.findByText("Version 2")).toBeInTheDocument();
    expect(screen.getAllByText("Not shown for your role.").length).toBeGreaterThan(0);
    expect(screen.queryByRole("button", { name: "Edit classification" })).not.toBeInTheDocument();
    expect(
      screen.getByText("Editing the classification needs the full case view. Without it, the text fields would be cleared on save."),
    ).toBeInTheDocument();
  });

  it("offers no edit without cases:triage or on a closed case", async () => {
    const { rerender } = render(<CaseClassification {...BASE_PROPS} canWrite={false} />);
    expect(await screen.findByText("No classification is recorded for this case yet.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Edit classification" })).not.toBeInTheDocument();

    rerender(<CaseClassification {...BASE_PROPS} closed />);
    expect(screen.queryByRole("button", { name: "Edit classification" })).not.toBeInTheDocument();
    expect(screen.getByText("This case is closed, so its classification cannot be changed.")).toBeInTheDocument();
  });

  it("saves the next version with every field, starting from the version that was read", async () => {
    save.mockResolvedValue({ ...NONE, version: 1, triggers: ["boundary_escape"], event_kind: "incident" });
    const user = userEvent.setup();
    render(<CaseClassification {...BASE_PROPS} />);

    const dialog = await openEditor(user);
    await user.click(within(dialog).getByRole("checkbox", { name: "Boundary escape" }));
    await user.click(within(dialog).getByLabelText("Event"));
    await user.click(await screen.findByRole("option", { name: "Incident" }));
    await user.click(within(dialog).getByRole("button", { name: "Save new version" }));

    await waitFor(() =>
      expect(save).toHaveBeenCalledWith("case-1", {
        expected_version: 0,
        triggers: ["boundary_escape"],
        event_kind: "incident",
        internal_incident_option: false,
        classification_rationale: null,
        responsible_operator: null,
        affected_versions: null,
        approved_scope: null,
        scope_permission_source: null,
        containment: null,
        evidence_gap_plan: null,
        next_update_due_at: null,
      }),
    );
    expect(await screen.findByText("Version 1")).toBeInTheDocument();
    expect(toast.success).toHaveBeenCalledWith("Classification saved as version 1.");
  });

  it("requires a rationale when the internal incident option is chosen", async () => {
    const user = userEvent.setup();
    render(<CaseClassification {...BASE_PROPS} />);

    const dialog = await openEditor(user);
    await user.click(within(dialog).getByRole("checkbox", { name: "Internal incident option" }));
    await user.click(within(dialog).getByRole("button", { name: "Save new version" }));

    expect(save).not.toHaveBeenCalled();
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Enter the rationale. It is required when the internal incident option is chosen.",
    );
  });

  it("reloads the displayed version and asks for a retry when the version changed underneath", async () => {
    save.mockRejectedValue(
      new ApiError(409, "classification changed: the current version is 3; reload and retry"),
    );
    get.mockResolvedValueOnce(NONE).mockResolvedValueOnce({ ...NONE, version: 3 });
    const user = userEvent.setup();
    render(<CaseClassification {...BASE_PROPS} />);

    const dialog = await openEditor(user);
    await user.click(within(dialog).getByRole("checkbox", { name: "Boundary escape" }));
    await user.click(within(dialog).getByRole("button", { name: "Save new version" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "The classification was changed by someone else. Reload it, check the current version and save again.",
    );
    await waitFor(() => expect(get).toHaveBeenCalledTimes(2));
    expect(save).toHaveBeenCalledWith("case-1", expect.objectContaining({ expected_version: 0 }));
  });
});
