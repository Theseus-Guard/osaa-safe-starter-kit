import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { ApiError, ReviewResponse, createReview, listReviews } from "../../api";
import { CaseReview } from "./CaseReview";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, createReview: vi.fn(), listReviews: vi.fn() };
});

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const create = vi.mocked(createReview);
const list = vi.mocked(listReviews);

// Newest first, as the backend returns them.
const NEWER: ReviewResponse = { id: "review-2", case_id: "case-1", outcome: "fail", note: "Missing logs" };
const OLDER: ReviewResponse = { id: "review-1", case_id: "case-1", outcome: "pass", note: null };

beforeEach(() => {
  create.mockReset();
  list.mockReset().mockResolvedValue([]);
  vi.mocked(toast.success).mockClear();
});

async function openDialog(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Record review" }));
  return screen.findByRole("dialog");
}

describe("CaseReview", () => {
  it("loads every review of the case and lists the outcome and the note", async () => {
    list.mockResolvedValue([NEWER, OLDER]);
    render(<CaseReview caseId="case-1" closed={false} canReview onRecorded={() => {}} />);

    expect(await screen.findByText("Missing logs")).toBeInTheDocument();
    expect(list).toHaveBeenCalledWith("case-1");
    const items = screen.getAllByRole("listitem");
    expect(items).toHaveLength(2);
    expect(within(items[0]).getByText("Fail")).toBeInTheDocument();
    expect(within(items[1]).getByText("Pass")).toBeInTheDocument();
    expect(within(items[1]).queryByText("Missing logs")).not.toBeInTheDocument();
  });

  it("shows an empty state when no review has been recorded", async () => {
    render(<CaseReview caseId="case-1" closed={false} canReview onRecorded={() => {}} />);

    expect(await screen.findByText("No review has been recorded for this case yet.")).toBeInTheDocument();
    expect(screen.queryByRole("listitem")).not.toBeInTheDocument();
  });

  it("shows the error when the reviews cannot be loaded", async () => {
    list.mockRejectedValue(new ApiError(404, "case not found"));
    render(<CaseReview caseId="case-1" closed={false} canReview onRecorded={() => {}} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("No case with that ID is available to your account.");
    expect(screen.queryByText("No review has been recorded for this case yet.")).not.toBeInTheDocument();
  });

  it("lists the reviews for a caller without reviews:write and hides the Record review button", async () => {
    list.mockResolvedValue([NEWER]);
    render(<CaseReview caseId="case-1" closed={false} canReview={false} onRecorded={() => {}} />);

    expect(await screen.findByText("Missing logs")).toBeInTheDocument();
    expect(screen.getByText("Reviews recorded on this case.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Record review" })).not.toBeInTheDocument();
  });

  it("asks for an outcome before submitting", async () => {
    const user = userEvent.setup();
    render(<CaseReview caseId="case-1" closed={false} canReview onRecorded={() => {}} />);

    const dialog = await openDialog(user);
    await user.click(within(dialog).getByRole("button", { name: "Submit review" }));

    expect(create).not.toHaveBeenCalled();
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Choose the outcome of the review.");
  });

  it("records the outcome with the trimmed note, reloads the list and reports the result", async () => {
    const stored: ReviewResponse = { id: "review-3", case_id: "case-1", outcome: "fail", note: "Missing logs" };
    list.mockResolvedValueOnce([]).mockResolvedValueOnce([stored]);
    create.mockResolvedValue(stored);
    const onRecorded = vi.fn();
    const user = userEvent.setup();
    render(<CaseReview caseId="case-1" closed={false} canReview onRecorded={onRecorded} />);

    await screen.findByText("No review has been recorded for this case yet.");
    const dialog = await openDialog(user);
    await user.click(within(dialog).getByLabelText("Outcome"));
    await user.click(await screen.findByRole("option", { name: "Fail" }));
    await user.type(within(dialog).getByLabelText("Note"), "  Missing logs  ");
    await user.click(within(dialog).getByRole("button", { name: "Submit review" }));

    await waitFor(() => expect(onRecorded).toHaveBeenCalledTimes(1));
    expect(create).toHaveBeenCalledWith("case-1", { outcome: "fail", note: "Missing logs" });
    expect(toast.success).toHaveBeenCalledWith("Review recorded.");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    // The list is loaded again from the server, so the new review appears with the stored ones.
    expect(await screen.findByText("Missing logs")).toBeInTheDocument();
    expect(list).toHaveBeenCalledTimes(2);
    expect(screen.queryByText("No review has been recorded for this case yet.")).not.toBeInTheDocument();
  });

  it("sends no note when the note is blank", async () => {
    create.mockResolvedValue({ id: "review-1", case_id: "case-1", outcome: "pass", note: null });
    const user = userEvent.setup();
    render(<CaseReview caseId="case-1" closed={false} canReview onRecorded={() => {}} />);

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
    render(<CaseReview caseId="case-1" closed={false} canReview onRecorded={() => {}} />);

    const dialog = await openDialog(user);
    await user.click(within(dialog).getByLabelText("Outcome"));
    await user.click(await screen.findByRole("option", { name: "Pass" }));
    await user.click(within(dialog).getByRole("button", { name: "Submit review" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "Only an assigned reviewer can record a review on this case.",
    );
    // A refused review is not stored, so the list is not loaded again.
    expect(list).toHaveBeenCalledTimes(1);
  });

  it("shows the server refusal when the case is closed", async () => {
    create.mockRejectedValue(new ApiError(409, "case is closed"));
    const user = userEvent.setup();
    render(<CaseReview caseId="case-1" closed={false} canReview onRecorded={() => {}} />);

    const dialog = await openDialog(user);
    await user.click(within(dialog).getByLabelText("Outcome"));
    await user.click(await screen.findByRole("option", { name: "Pass" }));
    await user.click(within(dialog).getByRole("button", { name: "Submit review" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("This case is closed.");
  });

  it("offers no form on a closed case but still lists its reviews", async () => {
    list.mockResolvedValue([NEWER]);
    render(<CaseReview caseId="case-1" closed canReview onRecorded={() => {}} />);

    expect(screen.getByText("This case is closed, so no review can be recorded.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Record review" })).not.toBeInTheDocument();
    expect(await screen.findByText("Missing logs")).toBeInTheDocument();
  });
});
