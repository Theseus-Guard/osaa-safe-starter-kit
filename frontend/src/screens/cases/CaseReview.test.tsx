import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { ApiError, ReviewResponse, createReview } from "../../api";
import { CaseReview } from "./CaseReview";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, createReview: vi.fn() };
});

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const create = vi.mocked(createReview);

beforeEach(() => {
  create.mockReset();
  vi.mocked(toast.success).mockClear();
});

async function openDialog(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Record review" }));
  return screen.findByRole("dialog");
}

describe("CaseReview", () => {
  it("asks for an outcome before submitting", async () => {
    const user = userEvent.setup();
    render(<CaseReview caseId="case-1" closed={false} onRecorded={() => {}} />);

    const dialog = await openDialog(user);
    await user.click(within(dialog).getByRole("button", { name: "Submit review" }));

    expect(create).not.toHaveBeenCalled();
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Choose the outcome of the review.");
  });

  it("records the outcome with the trimmed note, lists it for this session and reports the result", async () => {
    const stored: ReviewResponse = { id: "review-1", case_id: "case-1", outcome: "fail", note: "Missing logs" };
    create.mockResolvedValue(stored);
    const onRecorded = vi.fn();
    const user = userEvent.setup();
    render(<CaseReview caseId="case-1" closed={false} onRecorded={onRecorded} />);

    const dialog = await openDialog(user);
    await user.click(within(dialog).getByLabelText("Outcome"));
    await user.click(await screen.findByRole("option", { name: "Fail" }));
    await user.type(within(dialog).getByLabelText("Note"), "  Missing logs  ");
    await user.click(within(dialog).getByRole("button", { name: "Submit review" }));

    await waitFor(() => expect(onRecorded).toHaveBeenCalledTimes(1));
    expect(create).toHaveBeenCalledWith("case-1", { outcome: "fail", note: "Missing logs" });
    expect(toast.success).toHaveBeenCalledWith("Review recorded.");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.getByText("Recorded in this session")).toBeInTheDocument();
    expect(screen.getByText("Fail")).toBeInTheDocument();
    expect(screen.getByText("Missing logs")).toBeInTheDocument();
  });

  it("sends no note when the note is blank", async () => {
    create.mockResolvedValue({ id: "review-1", case_id: "case-1", outcome: "pass", note: null });
    const user = userEvent.setup();
    render(<CaseReview caseId="case-1" closed={false} onRecorded={() => {}} />);

    const dialog = await openDialog(user);
    await user.click(within(dialog).getByLabelText("Outcome"));
    await user.click(await screen.findByRole("option", { name: "Pass" }));
    await user.type(within(dialog).getByLabelText("Note"), "   ");
    await user.click(within(dialog).getByRole("button", { name: "Submit review" }));

    await waitFor(() => expect(create).toHaveBeenCalledWith("case-1", { outcome: "pass", note: null }));
  });

  it("shows the server refusal when the caller is not assigned to the case", async () => {
    create.mockRejectedValue(new ApiError(403, "only an assigned reviewer can record a review"));
    const user = userEvent.setup();
    render(<CaseReview caseId="case-1" closed={false} onRecorded={() => {}} />);

    const dialog = await openDialog(user);
    await user.click(within(dialog).getByLabelText("Outcome"));
    await user.click(await screen.findByRole("option", { name: "Pass" }));
    await user.click(within(dialog).getByRole("button", { name: "Submit review" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Only an assigned reviewer can record a review on this case.",
    );
    expect(screen.queryByText("Recorded in this session")).not.toBeInTheDocument();
  });

  it("shows the server refusal when the case is closed", async () => {
    create.mockRejectedValue(new ApiError(409, "case is closed"));
    const user = userEvent.setup();
    render(<CaseReview caseId="case-1" closed={false} onRecorded={() => {}} />);

    const dialog = await openDialog(user);
    await user.click(within(dialog).getByLabelText("Outcome"));
    await user.click(await screen.findByRole("option", { name: "Pass" }));
    await user.click(within(dialog).getByRole("button", { name: "Submit review" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("This case is closed.");
  });

  it("offers no form on a closed case", () => {
    render(<CaseReview caseId="case-1" closed onRecorded={() => {}} />);

    expect(screen.getByText("This case is closed, so no review can be recorded.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Record review" })).not.toBeInTheDocument();
  });
});
