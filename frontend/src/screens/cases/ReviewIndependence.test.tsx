import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import {
  ApiError,
  ReviewAppeal,
  ReviewConflict,
  ReviewRecusal,
  declareReviewConflict,
  fileReviewAppeal,
  listReviewAppeals,
  listReviewConflicts,
  listReviewRecusals,
  recuseFromReviewRound,
} from "../../api";
import { ReviewIndependence } from "./ReviewIndependence";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return {
    ...actual,
    listReviewConflicts: vi.fn(),
    listReviewRecusals: vi.fn(),
    listReviewAppeals: vi.fn(),
    declareReviewConflict: vi.fn(),
    recuseFromReviewRound: vi.fn(),
    fileReviewAppeal: vi.fn(),
  };
});

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const listConflicts = vi.mocked(listReviewConflicts);
const listRecusals = vi.mocked(listReviewRecusals);
const listAppeals = vi.mocked(listReviewAppeals);
const declare = vi.mocked(declareReviewConflict);
const recuse = vi.mocked(recuseFromReviewRound);
const appeal = vi.mocked(fileReviewAppeal);

const ROUND_ID = "round-1";
const REPLACEMENT = "11111111-2222-4333-8444-555555555555";
const INDEPENDENT = "66666666-7777-4888-8999-aaaaaaaaaaaa";
const STAMP = "2026-11-01T09:00:00Z";

const CURRENT_CONFLICT: ReviewConflict = {
  id: "conflict-2",
  round_id: ROUND_ID,
  reviewer_id: "user-1",
  kind: "declared",
  description: "Built the system under review",
  supersedes_id: "conflict-1",
  created_at: STAMP,
  current: true,
};
const SUPERSEDED_CONFLICT: ReviewConflict = {
  ...CURRENT_CONFLICT,
  id: "conflict-1",
  description: "First wording",
  supersedes_id: null,
  current: false,
};
const HIDDEN_CONFLICT: ReviewConflict = {
  ...CURRENT_CONFLICT,
  id: "conflict-3",
  reviewer_id: "user-2",
  description: null,
  supersedes_id: null,
};
const RECUSAL: ReviewRecusal = {
  id: "recusal-1",
  round_id: ROUND_ID,
  reviewer_id: "user-1",
  replacement_id: REPLACEMENT,
  reason: "Too close to the author",
  supersedes_id: null,
  created_at: STAMP,
  current: true,
};
const APPEAL: ReviewAppeal = {
  id: "appeal-1",
  round_id: ROUND_ID,
  filed_by: "user-9",
  independent_reviewer_id: INDEPENDENT,
  grounds: "The model layer was not tested",
  created_at: STAMP,
};

beforeEach(() => {
  listConflicts.mockReset().mockResolvedValue([]);
  listRecusals.mockReset().mockResolvedValue([]);
  listAppeals.mockReset().mockResolvedValue([]);
  declare.mockReset();
  recuse.mockReset();
  appeal.mockReset();
  vi.mocked(toast.success).mockClear();
});

describe("ReviewIndependence", () => {
  it("lists every conflict entry, marks the superseded one, and hides free text the caller may not read", async () => {
    listConflicts.mockResolvedValue([CURRENT_CONFLICT, SUPERSEDED_CONFLICT, HIDDEN_CONFLICT]);
    render(<ReviewIndependence roundId={ROUND_ID} canDeclare />);

    expect(await screen.findByText("Built the system under review")).toBeInTheDocument();
    expect(screen.getByText("First wording")).toBeInTheDocument();
    expect(screen.getByText("(superseded)", { exact: false })).toBeInTheDocument();
    expect(screen.getByText("(not visible to your account)")).toBeInTheDocument();
    expect(listConflicts).toHaveBeenCalledWith(ROUND_ID);
  });

  it("shows the recusal with its replacement and the appeal with its independent reviewer and the no-decision note", async () => {
    listRecusals.mockResolvedValue([RECUSAL]);
    listAppeals.mockResolvedValue([APPEAL]);
    render(<ReviewIndependence roundId={ROUND_ID} canDeclare={false} />);

    expect(await screen.findByText("Too close to the author")).toBeInTheDocument();
    expect(screen.getByText(REPLACEMENT)).toBeInTheDocument();
    expect(screen.getByText("The model layer was not tested")).toBeInTheDocument();
    expect(screen.getByText(INDEPENDENT)).toBeInTheDocument();
    expect(screen.getByText("The product does not decide it.", { exact: false })).toBeInTheDocument();
  });

  it("offers the conflict and recusal forms only when the caller may declare, and always offers the appeal form", async () => {
    const { unmount } = render(<ReviewIndependence roundId={ROUND_ID} canDeclare={false} />);
    expect(await screen.findByRole("form", { name: "File an appeal" })).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Declare a conflict" })).not.toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Withdraw from this round" })).not.toBeInTheDocument();
    unmount();

    render(<ReviewIndependence roundId={ROUND_ID} canDeclare />);
    expect(await screen.findByRole("form", { name: "Declare a conflict" })).toBeInTheDocument();
    expect(screen.getByRole("form", { name: "Withdraw from this round" })).toBeInTheDocument();
  });

  it("declares a conflict with the trimmed description, then reloads the entries", async () => {
    declare.mockResolvedValue(CURRENT_CONFLICT);
    const user = userEvent.setup();
    render(<ReviewIndependence roundId={ROUND_ID} canDeclare />);

    const form = await screen.findByRole("form", { name: "Declare a conflict" });
    await user.type(within(form).getByLabelText("Conflict"), "  Built the system under review  ");
    await user.click(within(form).getByRole("button", { name: "Declare conflict" }));

    await waitFor(() =>
      expect(declare).toHaveBeenCalledWith(ROUND_ID, { kind: "declared", description: "Built the system under review" }),
    );
    expect(toast.success).toHaveBeenCalledWith("Conflict declared.");
    await waitFor(() => expect(listConflicts).toHaveBeenCalledTimes(2));
  });

  it("does not send an empty conflict description", async () => {
    const user = userEvent.setup();
    render(<ReviewIndependence roundId={ROUND_ID} canDeclare />);

    const form = await screen.findByRole("form", { name: "Declare a conflict" });
    await user.type(within(form).getByLabelText("Conflict"), "   ");
    await user.click(within(form).getByRole("button", { name: "Declare conflict" }));

    expect(declare).not.toHaveBeenCalled();
    expect(await within(form).findByRole("alert")).toHaveTextContent("Describe the conflict before you declare it.");
  });

  it("shows the refusal for a second declaration in words, not as a raw code", async () => {
    declare.mockRejectedValue(new ApiError(409, "conflict_already_declared"));
    const user = userEvent.setup();
    render(<ReviewIndependence roundId={ROUND_ID} canDeclare />);

    const form = await screen.findByRole("form", { name: "Declare a conflict" });
    await user.type(within(form).getByLabelText("Conflict"), "Again");
    await user.click(within(form).getByRole("button", { name: "Declare conflict" }));

    expect(await within(form).findByRole("alert")).toHaveTextContent("You have already declared a conflict");
    expect(toast.success).not.toHaveBeenCalled();
  });

  it("refuses a replacement that is not a user ID before sending anything", async () => {
    const user = userEvent.setup();
    render(<ReviewIndependence roundId={ROUND_ID} canDeclare />);

    const form = await screen.findByRole("form", { name: "Withdraw from this round" });
    await user.type(within(form).getByLabelText("Replacement reviewer ID"), "someone");
    await user.click(within(form).getByRole("button", { name: "Withdraw and name replacement" }));

    expect(recuse).not.toHaveBeenCalled();
    expect(await within(form).findByRole("alert")).toHaveTextContent("Enter the user ID (UUID) of the replacement reviewer.");
  });

  it("names the replacement and sends no reason when the reason is blank", async () => {
    recuse.mockResolvedValue(RECUSAL);
    const user = userEvent.setup();
    render(<ReviewIndependence roundId={ROUND_ID} canDeclare />);

    const form = await screen.findByRole("form", { name: "Withdraw from this round" });
    await user.type(within(form).getByLabelText("Replacement reviewer ID"), REPLACEMENT);
    await user.type(within(form).getByLabelText("Recusal reason"), "   ");
    await user.click(within(form).getByRole("button", { name: "Withdraw and name replacement" }));

    await waitFor(() => expect(recuse).toHaveBeenCalledWith(ROUND_ID, { replacement_id: REPLACEMENT, reason: null }));
    expect(toast.success).toHaveBeenCalledWith("Recusal recorded.");
  });

  it("shows the refusal when the replacement has a declared conflict", async () => {
    recuse.mockRejectedValue(
      new ApiError(422, "the replacement has a declared conflict on this review round"),
    );
    const user = userEvent.setup();
    render(<ReviewIndependence roundId={ROUND_ID} canDeclare />);

    const form = await screen.findByRole("form", { name: "Withdraw from this round" });
    await user.type(within(form).getByLabelText("Replacement reviewer ID"), REPLACEMENT);
    await user.click(within(form).getByRole("button", { name: "Withdraw and name replacement" }));

    expect(await within(form).findByRole("alert")).toHaveTextContent("The replacement has a declared conflict");
  });

  it("requires grounds and an independent reviewer ID before filing an appeal", async () => {
    const user = userEvent.setup();
    render(<ReviewIndependence roundId={ROUND_ID} canDeclare={false} />);

    const form = await screen.findByRole("form", { name: "File an appeal" });
    await user.click(within(form).getByRole("button", { name: "File appeal" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent("Give the grounds of the appeal.");

    await user.type(within(form).getByLabelText("Appeal grounds"), "Wrong outcome");
    await user.click(within(form).getByRole("button", { name: "File appeal" }));
    expect(await within(form).findByRole("alert")).toHaveTextContent(
      "Enter the user ID (UUID) of the independent reviewer.",
    );
    expect(appeal).not.toHaveBeenCalled();
  });

  it("files an appeal with the trimmed grounds and the independent reviewer", async () => {
    appeal.mockResolvedValue(APPEAL);
    const user = userEvent.setup();
    render(<ReviewIndependence roundId={ROUND_ID} canDeclare={false} />);

    const form = await screen.findByRole("form", { name: "File an appeal" });
    await user.type(within(form).getByLabelText("Appeal grounds"), "  The model layer was not tested  ");
    await user.type(within(form).getByLabelText("Independent reviewer ID"), INDEPENDENT);
    await user.click(within(form).getByRole("button", { name: "File appeal" }));

    await waitFor(() =>
      expect(appeal).toHaveBeenCalledWith(ROUND_ID, {
        grounds: "The model layer was not tested",
        independent_reviewer_id: INDEPENDENT,
      }),
    );
    expect(toast.success).toHaveBeenCalledWith("Appeal filed.");
    await waitFor(() => expect(listAppeals).toHaveBeenCalledTimes(2));
  });

  it("shows the refusal when the independent reviewer took part in the round", async () => {
    appeal.mockRejectedValue(new ApiError(422, "the independent reviewer took part in this review round"));
    const user = userEvent.setup();
    render(<ReviewIndependence roundId={ROUND_ID} canDeclare={false} />);

    const form = await screen.findByRole("form", { name: "File an appeal" });
    await user.type(within(form).getByLabelText("Appeal grounds"), "Wrong outcome");
    await user.type(within(form).getByLabelText("Independent reviewer ID"), INDEPENDENT);
    await user.click(within(form).getByRole("button", { name: "File appeal" }));

    expect(await within(form).findByRole("alert")).toHaveTextContent("The independent reviewer took part in this review round.");
  });

  it("shows an error when the entries cannot be loaded", async () => {
    listConflicts.mockRejectedValue(new ApiError(404, "review not found"));
    render(<ReviewIndependence roundId={ROUND_ID} canDeclare />);

    expect(await screen.findByRole("alert")).toHaveTextContent("That review does not exist, or your account cannot see its case.");
  });
});
