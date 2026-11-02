import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { ApiError, ReviewRound, listReviewRounds, openReviewRound, setReviewLayer } from "../../api";
import { CaseReview } from "./CaseReview";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return {
    ...actual,
    listReviewRounds: vi.fn(),
    openReviewRound: vi.fn(),
    setReviewLayer: vi.fn(),
    // The conflict, recusal and appeal lists of each round. Empty unless a test sets them.
    listReviewConflicts: vi.fn().mockResolvedValue([]),
    listReviewRecusals: vi.fn().mockResolvedValue([]),
    listReviewAppeals: vi.fn().mockResolvedValue([]),
    declareReviewConflict: vi.fn(),
    recuseFromReviewRound: vi.fn(),
    fileReviewAppeal: vi.fn(),
  };
});

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const list = vi.mocked(listReviewRounds);
const open = vi.mocked(openReviewRound);
const setLayer = vi.mocked(setReviewLayer);

const LAYER_KEYS = [
  "model",
  "instructions",
  "safeguards",
  "tools",
  "environment",
  "monitoring",
  "human-operations",
  "supply-chain",
  "serving-path",
  "authorization-and-commitment",
  "workflow-composition",
];

// A round at the given revision. The model layer has the given outcome (fail with a reason by default, or null for
// not set); the other ten layers are not set.
function roundAt(revision: number, modelOutcome: ReviewRound["layers"][number]["outcome"] = "fail"): ReviewRound {
  const hasReason = modelOutcome !== null && modelOutcome !== "pass";
  return {
    id: "round-1",
    case_id: "case-1",
    status: "open",
    revision,
    opened_at: "2026-11-01T09:00:00Z",
    closed_at: null,
    layers: LAYER_KEYS.map((layer, index) => ({
      layer,
      outcome: index === 0 ? modelOutcome : null,
      reason: index === 0 && hasReason ? "Missing logs" : null,
      evidence_ids: [],
      set_at: index === 0 ? "2026-11-01T09:05:00Z" : null,
    })),
  };
}

beforeEach(() => {
  list.mockReset().mockResolvedValue([]);
  open.mockReset();
  setLayer.mockReset();
  vi.mocked(toast.success).mockClear();
});

async function openLayerDialog(user: ReturnType<typeof userEvent.setup>, layerLabel: string) {
  await user.click(screen.getByRole("button", { name: `Set ${layerLabel}` }));
  return screen.findByRole("dialog");
}

async function chooseOutcome(user: ReturnType<typeof userEvent.setup>, dialog: HTMLElement, outcome: string) {
  await user.click(within(dialog).getByLabelText("Outcome"));
  await user.click(await screen.findByRole("option", { name: outcome }));
}

describe("CaseReview", () => {
  it("shows the eleven layers of the round with their outcomes and reasons", async () => {
    list.mockResolvedValue([roundAt(3)]);
    render(<CaseReview caseId="case-1" closed={false} canReview onRecorded={() => {}} />);

    expect(await screen.findByText("Revision 3, open")).toBeInTheDocument();
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(11);
    expect(within(items[0]).getByText("Model")).toBeInTheDocument();
    expect(within(items[0]).getByText("Fail")).toBeInTheDocument();
    expect(within(items[0]).getByText("Missing logs")).toBeInTheDocument();
    expect(within(items[10]).getByText("Workflow composition")).toBeInTheDocument();
    expect(within(items[10]).getByText("Not set")).toBeInTheDocument();
    expect(list).toHaveBeenCalledWith("case-1");
  });

  it("opens a round when none exists, and reloads the rounds", async () => {
    list.mockResolvedValueOnce([]).mockResolvedValueOnce([roundAt(1)]);
    open.mockResolvedValue(roundAt(1));
    const onRecorded = vi.fn();
    const user = userEvent.setup();
    render(<CaseReview caseId="case-1" closed={false} canReview onRecorded={onRecorded} />);

    expect(await screen.findByText("No review round has been opened for this case yet.")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Open review round" }));

    await waitFor(() => expect(open).toHaveBeenCalledWith("case-1"));
    await waitFor(() => expect(onRecorded).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("Revision 1, open")).toBeInTheDocument();
  });

  it("asks for a reason before saving a non-pass outcome and sends nothing", async () => {
    list.mockResolvedValue([roundAt(3, "not_applicable")]);
    const user = userEvent.setup();
    render(<CaseReview caseId="case-1" closed={false} canReview onRecorded={() => {}} />);

    await screen.findByText("Revision 3, open");
    const dialog = await openLayerDialog(user, "Instructions");
    await chooseOutcome(user, dialog, "Fail");
    await user.click(within(dialog).getByRole("button", { name: "Save layer" }));

    expect(setLayer).not.toHaveBeenCalled();
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("A reason is required for this outcome.");
  });

  it("refuses a pass without evidence and sends nothing", async () => {
    list.mockResolvedValue([roundAt(3, null)]);
    const user = userEvent.setup();
    render(<CaseReview caseId="case-1" closed={false} canReview onRecorded={() => {}} />);

    await screen.findByText("Revision 3, open");
    const dialog = await openLayerDialog(user, "Model");
    await chooseOutcome(user, dialog, "Pass");
    await user.click(within(dialog).getByRole("button", { name: "Save layer" }));

    expect(setLayer).not.toHaveBeenCalled();
    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "A pass needs at least one evidence item of this case.",
    );
  });

  it("saves a pass that cites evidence, with the revision the editor read", async () => {
    const evidenceId = "5b0c1d2e-3f4a-4b5c-8d6e-7f8091a2b3c4";
    list.mockResolvedValueOnce([roundAt(3, null)]).mockResolvedValueOnce([roundAt(4, "pass")]);
    setLayer.mockResolvedValue(roundAt(4, "pass"));
    const onRecorded = vi.fn();
    const user = userEvent.setup();
    render(<CaseReview caseId="case-1" closed={false} canReview onRecorded={onRecorded} />);

    await screen.findByText("Revision 3, open");
    const dialog = await openLayerDialog(user, "Model");
    await chooseOutcome(user, dialog, "Pass");
    await user.type(within(dialog).getByLabelText("Evidence IDs"), evidenceId);
    await user.click(within(dialog).getByRole("button", { name: "Save layer" }));

    await waitFor(() =>
      expect(setLayer).toHaveBeenCalledWith("round-1", "model", {
        outcome: "pass",
        reason: null,
        evidence_ids: [evidenceId],
        expected_revision: 3,
      }),
    );
    expect(toast.success).toHaveBeenCalledWith("Review layer saved.");
    expect(onRecorded).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("shows the refusal the server gives for a pass without an available evidence item", async () => {
    list.mockResolvedValue([roundAt(3, null)]);
    setLayer.mockRejectedValueOnce(new ApiError(422, "a pass needs at least one evidence item of this case"));
    const user = userEvent.setup();
    render(<CaseReview caseId="case-1" closed={false} canReview onRecorded={() => {}} />);

    await screen.findByText("Revision 3, open");
    const dialog = await openLayerDialog(user, "Model");
    await chooseOutcome(user, dialog, "Pass");
    await user.type(within(dialog).getByLabelText("Evidence IDs"), "5b0c1d2e-3f4a-4b5c-8d6e-7f8091a2b3c4");
    await user.click(within(dialog).getByRole("button", { name: "Save layer" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "A pass needs at least one evidence item of this case.",
    );
  });

  it("sends the trimmed reason and the evidence IDs for a fail", async () => {
    list.mockResolvedValue([roundAt(3, "pass")]);
    setLayer.mockResolvedValue(roundAt(4));
    const evidence = "11111111-2222-4333-8444-555555555555";
    const user = userEvent.setup();
    render(<CaseReview caseId="case-1" closed={false} canReview onRecorded={() => {}} />);

    await screen.findByText("Revision 3, open");
    const dialog = await openLayerDialog(user, "Model");
    await chooseOutcome(user, dialog, "Fail");
    await user.type(within(dialog).getByLabelText("Reason"), "  Missing logs  ");
    await user.type(within(dialog).getByLabelText("Evidence IDs"), evidence);
    await user.click(within(dialog).getByRole("button", { name: "Save layer" }));

    await waitFor(() =>
      expect(setLayer).toHaveBeenCalledWith("round-1", "model", {
        outcome: "fail",
        reason: "Missing logs",
        evidence_ids: [evidence],
        expected_revision: 3,
      }),
    );
  });

  it("shows the conflict message with the current revision and reloads the round", async () => {
    list.mockResolvedValueOnce([roundAt(3)]).mockResolvedValueOnce([roundAt(5)]);
    setLayer.mockRejectedValue(new ApiError(409, "revision_conflict", null, 5));
    const user = userEvent.setup();
    render(<CaseReview caseId="case-1" closed={false} canReview onRecorded={() => {}} />);

    await screen.findByText("Revision 3, open");
    const dialog = await openLayerDialog(user, "Model");
    await chooseOutcome(user, dialog, "Insufficient evidence");
    await user.type(within(dialog).getByLabelText("Reason"), "Needs the export");
    await user.click(within(dialog).getByRole("button", { name: "Save layer" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The review was changed by someone else, so nothing was saved.");
    expect(alert).toHaveTextContent("The current revision is 5.");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(await screen.findByText("Revision 5, open")).toBeInTheDocument();
    expect(toast.success).not.toHaveBeenCalled();
  });

  it("shows the server refusal when the caller is not assigned to the case", async () => {
    list.mockResolvedValue([roundAt(3)]);
    setLayer.mockRejectedValue(new ApiError(403, "only an assigned reviewer can record a review"));
    const user = userEvent.setup();
    render(<CaseReview caseId="case-1" closed={false} canReview onRecorded={() => {}} />);

    await screen.findByText("Revision 3, open");
    const dialog = await openLayerDialog(user, "Model");
    await chooseOutcome(user, dialog, "Fail");
    await user.type(within(dialog).getByLabelText("Reason"), "Gap found");
    await user.click(within(dialog).getByRole("button", { name: "Save layer" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Only an assigned reviewer can record a review on this case.",
    );
  });

  it("lists the rounds read-only for a caller without reviews:write", async () => {
    list.mockResolvedValue([roundAt(3)]);
    render(<CaseReview caseId="case-1" closed={false} canReview={false} onRecorded={() => {}} />);

    expect(await screen.findByText("Revision 3, open")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open review round" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Set Model" })).not.toBeInTheDocument();
  });

  it("offers no change on a closed case but still shows its rounds", async () => {
    list.mockResolvedValue([roundAt(3)]);
    render(<CaseReview caseId="case-1" closed canReview onRecorded={() => {}} />);

    expect(screen.getByText("This case is closed, so no review can be recorded.")).toBeInTheDocument();
    expect(await screen.findByText("Revision 3, open")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Open review round" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Set Model" })).not.toBeInTheDocument();
  });

  it("shows the error when the rounds cannot be loaded", async () => {
    list.mockRejectedValue(new ApiError(404, "case not found"));
    render(<CaseReview caseId="case-1" closed={false} canReview onRecorded={() => {}} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("No case with that ID is available to your account.");
  });
});
