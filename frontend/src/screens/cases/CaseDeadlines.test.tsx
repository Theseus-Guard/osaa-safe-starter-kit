import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { CaseDetail, DeadlineTask, updateDeadline } from "../../api";
import { CaseDeadlines } from "./CaseDeadlines";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, updateDeadline: vi.fn() };
});

vi.mock("../../system-timezone", () => ({
  useSystemTimezone: () => ({ timezone: "UTC", effectiveFrom: null, error: null, setCurrent: () => {}, reload: () => {} }),
}));

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const update = vi.mocked(updateDeadline);

const TASKS: DeadlineTask[] = [
  {
    id: "task-1",
    kind: "preliminary_safe_report",
    due_at: "2026-11-02T06:00:00Z",
    status: "open",
    not_applicable_reason: null,
  },
  {
    id: "task-2",
    kind: "weekly_update",
    due_at: "2026-11-08T06:00:00Z",
    status: "done",
    not_applicable_reason: null,
  },
];

// The row of the open task, which is the first row in the table.
function openRow(): HTMLElement {
  return screen.getByText("Preliminary safe report").closest("tr") as HTMLElement;
}

beforeEach(() => {
  update.mockReset();
  vi.mocked(toast.success).mockClear();
  vi.mocked(toast.error).mockClear();
});

describe("CaseDeadlines", () => {
  it("shows each task with its status and the actions that apply", () => {
    render(<CaseDeadlines caseId="case-1" deadlines={TASKS} closed={false} onChanged={() => {}} />);

    expect(screen.getByText("Preliminary safe report")).toBeInTheDocument();
    const openRow = screen.getByText("Preliminary safe report").closest("tr") as HTMLElement;
    expect(openRow).toHaveTextContent("Mark done");
    expect(openRow).toHaveTextContent("Not applicable");
    expect(openRow).not.toHaveTextContent("Set open");

    const doneRow = screen.getByText("Weekly update").closest("tr") as HTMLElement;
    expect(doneRow).toHaveTextContent("Set open");
    expect(doneRow).not.toHaveTextContent("Mark done");
  });

  it("marks a task done and reports the updated case", async () => {
    const updated = { id: "case-1" } as CaseDetail;
    update.mockResolvedValue(updated);
    const onChanged = vi.fn();
    const user = userEvent.setup();
    render(<CaseDeadlines caseId="case-1" deadlines={TASKS} closed={false} onChanged={onChanged} />);

    const openRow = screen.getByText("Preliminary safe report").closest("tr") as HTMLElement;
    await user.click(openRow.querySelector("button") as HTMLButtonElement);

    expect(update).toHaveBeenCalledWith("case-1", "task-1", "done", undefined);
    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(updated));
    expect(toast.success).toHaveBeenCalledWith("Preliminary safe report is now done.");
  });

  it("asks for a reason before a task is marked not applicable", async () => {
    const user = userEvent.setup();
    render(<CaseDeadlines caseId="case-1" deadlines={TASKS} closed={false} onChanged={() => {}} />);

    await user.click(within(openRow()).getByRole("button", { name: "Not applicable" }));
    await user.click(screen.getByRole("button", { name: "Confirm" }));

    expect(update).not.toHaveBeenCalled();
    expect(toast.error).toHaveBeenCalledWith("Enter the reason the deadline does not apply.");
  });

  it("sends the trimmed reason with a not-applicable change", async () => {
    update.mockResolvedValue({ id: "case-1" } as CaseDetail);
    const user = userEvent.setup();
    render(<CaseDeadlines caseId="case-1" deadlines={TASKS} closed={false} onChanged={() => {}} />);

    await user.click(within(openRow()).getByRole("button", { name: "Not applicable" }));
    await user.type(screen.getByLabelText("Reason Preliminary safe report does not apply"), "  Not a reportable incident  ");
    await user.click(screen.getByRole("button", { name: "Confirm" }));

    await waitFor(() =>
      expect(update).toHaveBeenCalledWith("case-1", "task-1", "not_applicable", "Not a reportable incident"),
    );
  });

  it("shows the server message when the change is refused", async () => {
    const { ApiError } = await vi.importActual<typeof import("../../api")>("../../api");
    update.mockRejectedValue(new ApiError(403, "only a triager may mark a deadline not applicable"));
    const user = userEvent.setup();
    render(<CaseDeadlines caseId="case-1" deadlines={TASKS} closed={false} onChanged={() => {}} />);

    await user.click(within(openRow()).getByRole("button", { name: "Not applicable" }));
    await user.type(screen.getByLabelText("Reason Preliminary safe report does not apply"), "Not relevant");
    await user.click(screen.getByRole("button", { name: "Confirm" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("Only a triager can mark a deadline not applicable."),
    );
  });

  it("locks the actions on a closed case", () => {
    render(<CaseDeadlines caseId="case-1" deadlines={TASKS} closed onChanged={() => {}} />);

    for (const button of screen.getAllByRole("button", { name: /Mark done|Set open|Not applicable/ })) {
      expect(button).toBeDisabled();
    }
  });

  it("locks the actions on a task that is not stored yet", () => {
    const unstored: DeadlineTask = { ...TASKS[0], id: null };
    render(<CaseDeadlines caseId="case-1" deadlines={[unstored]} closed={false} onChanged={() => {}} />);

    expect(screen.getByRole("button", { name: "Mark done" })).toBeDisabled();
  });

  it("shows the recorded reason for a not-applicable task", () => {
    const na: DeadlineTask = { ...TASKS[0], status: "not_applicable", not_applicable_reason: "Out of scope" };
    render(<CaseDeadlines caseId="case-1" deadlines={[na]} closed={false} onChanged={() => {}} />);

    expect(screen.getByText("Reason: Out of scope")).toBeInTheDocument();
  });
});
