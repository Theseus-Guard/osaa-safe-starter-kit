import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { ApiError, CaseLink, addCaseLink, listCaseLinks, removeCaseLink } from "../../api";
import { CaseLinks } from "./CaseLinks";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return {
    ...actual,
    listCaseLinks: vi.fn(),
    addCaseLink: vi.fn(),
    removeCaseLink: vi.fn(),
  };
});

vi.mock("../../system-timezone", () => ({
  useSystemTimezone: () => ({ timezone: "UTC", effectiveFrom: null, error: null, setCurrent: () => {}, reload: () => {} }),
}));

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const list = vi.mocked(listCaseLinks);
const add = vi.mocked(addCaseLink);
const remove = vi.mocked(removeCaseLink);

const OTHER_ID = "3f2a9c1e-4b7d-4e8a-9c2f-1a2b3c4d5e6f";

const LINK: CaseLink = {
  linked_case_id: OTHER_ID,
  linked_case_title: "Ministry portal exposure",
  created_at: "2026-11-01T06:00:00Z",
};

// A link to a case the caller cannot see: no ID and no title come back.
const HIDDEN: CaseLink = { linked_case_id: null, linked_case_title: null, created_at: "2026-11-01T07:00:00Z" };

beforeEach(() => {
  list.mockReset();
  add.mockReset();
  remove.mockReset();
  vi.mocked(toast.success).mockClear();
  vi.mocked(toast.error).mockClear();
});

describe("CaseLinks", () => {
  it("shows an empty state when no case is linked", async () => {
    list.mockResolvedValue([]);
    render(<CaseLinks caseId="case-1" closed={false} canWrite />);

    expect(await screen.findByText("No case is linked to this case yet.")).toBeInTheDocument();
  });

  it("lists the linked case by title and ID", async () => {
    list.mockResolvedValue([LINK]);
    render(<CaseLinks caseId="case-1" closed={false} canWrite />);

    const row = (await screen.findByText("Ministry portal exposure")).closest("tr") as HTMLElement;
    expect(within(row).getByText(OTHER_ID)).toBeInTheDocument();
    expect(list).toHaveBeenCalledWith("case-1");
  });

  it("shows a link to a case the caller cannot see without an ID, a title or a remove button", async () => {
    list.mockResolvedValue([HIDDEN]);
    render(<CaseLinks caseId="case-1" closed={false} canWrite />);

    const row = (await screen.findByText("Not available to your account")).closest("tr") as HTMLElement;
    expect(within(row).queryByRole("button", { name: /^Remove/ })).not.toBeInTheDocument();
    expect(within(row).getByText("-")).toBeInTheDocument();
  });

  it("reads links without the add form or remove buttons when the caller cannot write", async () => {
    list.mockResolvedValue([LINK]);
    render(<CaseLinks caseId="case-1" closed={false} canWrite={false} />);

    expect(await screen.findByText("Ministry portal exposure")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Remove/ })).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Case ID to link/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Link case" })).not.toBeInTheDocument();
  });

  it("locks removal and hides the add form on a closed case", async () => {
    list.mockResolvedValue([LINK]);
    render(<CaseLinks caseId="case-1" closed canWrite />);

    expect(await screen.findByRole("button", { name: /^Remove/ })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Link case" })).not.toBeInTheDocument();
  });

  it("shows an error when the links cannot be loaded", async () => {
    list.mockRejectedValue(new ApiError(404, "case not found"));
    render(<CaseLinks caseId="case-1" closed={false} canWrite />);

    expect(await screen.findByText("No case with that ID is available to your account.")).toBeInTheDocument();
  });

  it("refuses an ID that is not a UUID without sending a request", async () => {
    const user = userEvent.setup();
    list.mockResolvedValue([]);
    render(<CaseLinks caseId="case-1" closed={false} canWrite />);

    await user.type(await screen.findByLabelText(/^Case ID to link/), "not-an-id");
    await user.click(screen.getByRole("button", { name: "Link case" }));

    expect(await screen.findByText(/Enter the case ID as a UUID/)).toBeInTheDocument();
    expect(add).not.toHaveBeenCalled();
  });

  it("links a case by its ID, trims the input, announces that nothing else changed and reloads", async () => {
    const user = userEvent.setup();
    list.mockResolvedValueOnce([]).mockResolvedValueOnce([LINK]);
    add.mockResolvedValue(LINK);
    render(<CaseLinks caseId="case-1" closed={false} canWrite />);

    await user.type(await screen.findByLabelText(/^Case ID to link/), `  ${OTHER_ID}  `);
    await user.click(screen.getByRole("button", { name: "Link case" }));

    await waitFor(() => expect(add).toHaveBeenCalledWith("case-1", OTHER_ID));
    expect(toast.success).toHaveBeenCalledWith("Cases linked. Linking changes no task, deadline or status.");
    expect(await screen.findByText("Ministry portal exposure")).toBeInTheDocument();
    expect(list).toHaveBeenCalledTimes(2);
  });

  it("shows the message for a refused link", async () => {
    const user = userEvent.setup();
    list.mockResolvedValue([]);
    add.mockRejectedValue(new ApiError(409, "cases are already linked"));
    render(<CaseLinks caseId="case-1" closed={false} canWrite />);

    await user.type(await screen.findByLabelText(/^Case ID to link/), OTHER_ID);
    await user.click(screen.getByRole("button", { name: "Link case" }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("These two cases are already linked."));
  });

  it("removes a link by the linked case ID and reloads", async () => {
    const user = userEvent.setup();
    list.mockResolvedValueOnce([LINK]).mockResolvedValueOnce([]);
    remove.mockResolvedValue(undefined);
    render(<CaseLinks caseId="case-1" closed={false} canWrite />);

    await user.click(await screen.findByRole("button", { name: /^Remove/ }));

    await waitFor(() => expect(remove).toHaveBeenCalledWith("case-1", OTHER_ID));
    expect(toast.success).toHaveBeenCalledWith("The link was removed. No task, deadline or status changed.");
    expect(await screen.findByText("No case is linked to this case yet.")).toBeInTheDocument();
  });

  it("shows the message when a removal is refused", async () => {
    const user = userEvent.setup();
    list.mockResolvedValue([LINK]);
    remove.mockRejectedValue(new ApiError(404, "link not found"));
    render(<CaseLinks caseId="case-1" closed={false} canWrite />);

    await user.click(await screen.findByRole("button", { name: /^Remove/ }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("These two cases are not linked."));
  });
});
