import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { ApiError, AssignableUser, Assignment, addAssignment, listAssignableUsers, listAssignments, removeAssignment } from "../../api";
import { CaseAssignments } from "./CaseAssignments";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return {
    ...actual,
    listAssignments: vi.fn(),
    removeAssignment: vi.fn(),
    addAssignment: vi.fn(),
    listAssignableUsers: vi.fn(),
  };
});

vi.mock("../../system-timezone", () => ({
  useSystemTimezone: () => ({ timezone: "UTC", effectiveFrom: null, error: null, setCurrent: () => {}, reload: () => {} }),
}));

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const list = vi.mocked(listAssignments);
const remove = vi.mocked(removeAssignment);
const add = vi.mocked(addAssignment);
const candidates = vi.mocked(listAssignableUsers);

const ASSIGNMENT: Assignment = {
  id: "assignment-1",
  case_id: "case-1",
  user_id: "user-2",
  username: "example-investigator",
  role: "investigator",
  assigned_by: "user-1",
  assigned_at: "2026-11-01T06:00:00Z",
};

// A user who is not assigned yet, and one in another assignable role.
const INVESTIGATOR: AssignableUser = {
  id: "user-2",
  username: "example-investigator",
  first_name: null,
  last_name: null,
  role: "investigator",
};
const REVIEWER: AssignableUser = {
  id: "user-3",
  username: "example-reviewer",
  first_name: "Ada",
  last_name: "Lovelace",
  role: "reviewer",
};

beforeEach(() => {
  list.mockReset();
  remove.mockReset();
  add.mockReset();
  candidates.mockReset().mockResolvedValue([]);
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
    list.mockRejectedValue(new ApiError(404, "case not found"));
    render(<CaseAssignments caseId="case-1" closed={false} canAssign />);

    expect(await screen.findByRole("alert")).toHaveTextContent("No case with that ID is available to your account.");
  });
});

describe("CaseAssignments assign picker", () => {
  it("offers the assignable users with their role and leaves out those already assigned", async () => {
    list.mockResolvedValue([ASSIGNMENT]);
    candidates.mockResolvedValue([INVESTIGATOR, REVIEWER]);
    const user = userEvent.setup();
    render(<CaseAssignments caseId="case-1" closed={false} canAssign />);

    await user.click(await screen.findByLabelText("Add a user"));

    expect(candidates).toHaveBeenCalledWith("case-1");
    expect(await screen.findByRole("option", { name: "Ada Lovelace (example-reviewer) - Reviewer" })).toBeInTheDocument();
    expect(screen.queryByRole("option", { name: "example-investigator - Investigator" })).not.toBeInTheDocument();
  });

  it("keeps Assign disabled until a user is chosen", async () => {
    list.mockResolvedValue([]);
    candidates.mockResolvedValue([INVESTIGATOR]);
    render(<CaseAssignments caseId="case-1" closed={false} canAssign />);

    expect(await screen.findByRole("button", { name: "Assign" })).toBeDisabled();
  });

  it("assigns the chosen user, reloads both lists and reports the result", async () => {
    list.mockResolvedValueOnce([]).mockResolvedValueOnce([{ ...ASSIGNMENT }]);
    candidates.mockResolvedValueOnce([INVESTIGATOR]).mockResolvedValueOnce([]);
    add.mockResolvedValue({ ...ASSIGNMENT });
    const user = userEvent.setup();
    render(<CaseAssignments caseId="case-1" closed={false} canAssign />);

    await user.click(await screen.findByLabelText("Add a user"));
    await user.click(await screen.findByRole("option", { name: "example-investigator - Investigator" }));
    await user.click(screen.getByRole("button", { name: "Assign" }));

    await waitFor(() => expect(add).toHaveBeenCalledWith("case-1", "user-2"));
    expect(toast.success).toHaveBeenCalledWith("example-investigator was assigned to the case.");
    expect(await screen.findByText("No other user can be assigned to this case.")).toBeInTheDocument();
    expect(list).toHaveBeenCalledTimes(2);
    expect(candidates).toHaveBeenCalledTimes(2);
  });

  it("shows the server message when the assignment is refused", async () => {
    list.mockResolvedValue([]);
    candidates.mockResolvedValue([INVESTIGATOR]);
    add.mockRejectedValue(new ApiError(422, "assignee must be an investigator or reviewer"));
    const user = userEvent.setup();
    render(<CaseAssignments caseId="case-1" closed={false} canAssign />);

    await user.click(await screen.findByLabelText("Add a user"));
    await user.click(await screen.findByRole("option", { name: "example-investigator - Investigator" }));
    await user.click(screen.getByRole("button", { name: "Assign" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("Only an investigator or a reviewer can be assigned to a case."),
    );
    expect(toast.success).not.toHaveBeenCalled();
    expect(list).toHaveBeenCalledTimes(1);
  });

  it("says so when no other user can be assigned", async () => {
    list.mockResolvedValue([]);
    candidates.mockResolvedValue([]);
    render(<CaseAssignments caseId="case-1" closed={false} canAssign />);

    expect(await screen.findByText("No other user can be assigned to this case.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Assign" })).not.toBeInTheDocument();
  });

  it("shows the picker error when the users cannot be loaded", async () => {
    list.mockResolvedValue([]);
    candidates.mockRejectedValue(new ApiError(403, "forbidden"));
    render(<CaseAssignments caseId="case-1" closed={false} canAssign />);

    expect(await screen.findByRole("alert")).toHaveTextContent("You do not have permission for this action.");
  });

  it("does not show the picker or load the users without cases:assign", async () => {
    list.mockResolvedValue([]);
    render(<CaseAssignments caseId="case-1" closed={false} canAssign={false} />);

    expect(await screen.findByText("No one is assigned to this case yet.")).toBeInTheDocument();
    expect(screen.queryByLabelText("Add a user")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Assign" })).not.toBeInTheDocument();
    expect(candidates).not.toHaveBeenCalled();
  });

  it("does not show the picker or load the users on a closed case", async () => {
    list.mockResolvedValue([ASSIGNMENT]);
    render(<CaseAssignments caseId="case-1" closed canAssign />);

    expect(await screen.findByRole("button", { name: "Remove" })).toBeDisabled();
    expect(screen.queryByLabelText("Add a user")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Assign" })).not.toBeInTheDocument();
    expect(candidates).not.toHaveBeenCalled();
  });
});
