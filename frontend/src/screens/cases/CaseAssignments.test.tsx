import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { Assignment, listAssignments, removeAssignment } from "../../api";
import { CaseAssignments } from "./CaseAssignments";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, listAssignments: vi.fn(), removeAssignment: vi.fn() };
});

vi.mock("../../system-timezone", () => ({
  useSystemTimezone: () => ({ timezone: "UTC", effectiveFrom: null, error: null, setCurrent: () => {}, reload: () => {} }),
}));

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const list = vi.mocked(listAssignments);
const remove = vi.mocked(removeAssignment);

const ASSIGNMENT: Assignment = {
  id: "assignment-1",
  case_id: "case-1",
  user_id: "user-2",
  username: "example-investigator",
  role: "investigator",
  assigned_by: "user-1",
  assigned_at: "2026-11-01T06:00:00Z",
};

beforeEach(() => {
  list.mockReset();
  remove.mockReset();
  vi.mocked(toast.success).mockClear();
  vi.mocked(toast.error).mockClear();
});

describe("CaseAssignments", () => {
  it("shows an empty state when nobody is assigned", async () => {
    list.mockResolvedValue([]);
    render(<CaseAssignments caseId="case-1" closed={false} canAssign />);

    expect(await screen.findByText("No one is assigned to this case yet.")).toBeInTheDocument();
  });

  it("lists the assignees with their role", async () => {
    list.mockResolvedValue([ASSIGNMENT]);
    render(<CaseAssignments caseId="case-1" closed={false} canAssign />);

    const row = (await screen.findByText("example-investigator")).closest("tr") as HTMLElement;
    expect(within(row).getByText("Investigator")).toBeInTheDocument();
    expect(list).toHaveBeenCalledWith("case-1");
  });

  it("removes an assignee and reloads the list", async () => {
    list.mockResolvedValueOnce([ASSIGNMENT]).mockResolvedValueOnce([]);
    remove.mockResolvedValue(undefined);
    const user = userEvent.setup();
    render(<CaseAssignments caseId="case-1" closed={false} canAssign />);

    await user.click(await screen.findByRole("button", { name: "Remove" }));

    await waitFor(() => expect(remove).toHaveBeenCalledWith("case-1", "user-2"));
    expect(await screen.findByText("No one is assigned to this case yet.")).toBeInTheDocument();
    expect(toast.success).toHaveBeenCalledWith("example-investigator was removed from the case.");
  });

  it("locks removal on a closed case", async () => {
    list.mockResolvedValue([ASSIGNMENT]);
    render(<CaseAssignments caseId="case-1" closed canAssign />);

    expect(await screen.findByRole("button", { name: "Remove" })).toBeDisabled();
  });

  it("locks removal for a caller without cases:assign", async () => {
    list.mockResolvedValue([ASSIGNMENT]);
    render(<CaseAssignments caseId="case-1" closed={false} canAssign={false} />);

    expect(await screen.findByRole("button", { name: "Remove" })).toBeDisabled();
    expect(list).toHaveBeenCalledWith("case-1");
  });

  it("shows the server message when the list cannot be loaded", async () => {
    const { ApiError } = await vi.importActual<typeof import("../../api")>("../../api");
    list.mockRejectedValue(new ApiError(404, "case not found"));
    render(<CaseAssignments caseId="case-1" closed={false} canAssign />);

    expect(await screen.findByRole("alert")).toHaveTextContent("No case with that ID is available to your account.");
  });
});
