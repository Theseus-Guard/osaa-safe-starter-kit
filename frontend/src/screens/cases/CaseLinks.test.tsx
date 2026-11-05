import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { ApiError, CaseLink, CaseSummary, addCaseLink, listCaseLinks, listCases, removeCaseLink } from "../../api";
import { CaseLinks } from "./CaseLinks";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return {
    ...actual,
    listCaseLinks: vi.fn(),
    addCaseLink: vi.fn(),
    removeCaseLink: vi.fn(),
    listCases: vi.fn(),
  };
});

vi.mock("../../system-timezone", () => ({
  useSystemTimezone: () => ({ timezone: "UTC", effectiveFrom: null, error: null, setCurrent: () => {}, reload: () => {} }),
}));

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const list = vi.mocked(listCaseLinks);
const add = vi.mocked(addCaseLink);
const remove = vi.mocked(removeCaseLink);
const findCases = vi.mocked(listCases);

const OTHER_ID = "3f2a9c1e-4b7d-4e8a-9c2f-1a2b3c4d5e6f";
const CASE_ID = "case-1";

const LINK: CaseLink = {
  linked_case_id: OTHER_ID,
  linked_case_title: "Ministry portal exposure",
  link_type: "related",
  reason: "Same exposure reported twice",
  share_visibility: false,
  primary_side: null,
  primary_case_id: null,
  created_at: "2026-11-01T06:00:00Z",
};

// A link to a case the caller cannot see: no ID and no title come back.
const HIDDEN: CaseLink = {
  linked_case_id: null,
  linked_case_title: null,
  link_type: "related",
  reason: null,
  share_visibility: false,
  primary_side: null,
  primary_case_id: null,
  created_at: "2026-11-01T07:00:00Z",
};

beforeEach(() => {
  list.mockReset();
  findCases.mockReset();
  add.mockReset();
  remove.mockReset();
  vi.mocked(toast.success).mockClear();
  vi.mocked(toast.error).mockClear();
});

async function chooseType(user: ReturnType<typeof userEvent.setup>, name: RegExp, option: string) {
  await user.click(screen.getByRole("combobox", { name }));
  await user.click(await screen.findByRole("option", { name: option }));
}

async function typeLinkedCase(user: ReturnType<typeof userEvent.setup>, value: string) {
  await user.type(await screen.findByLabelText(/^Case ID to link/), value);
}

async function typeReason(user: ReturnType<typeof userEvent.setup>, value: string) {
  await user.type(screen.getByLabelText(/^Link reason/), value);
}

describe("CaseLinks", () => {
  it("shows an empty state when no case is linked", async () => {
    list.mockResolvedValue([]);
    render(<CaseLinks caseId={CASE_ID} closed={false} canWrite />);

    expect(await screen.findByText("No case is linked to this case yet.")).toBeInTheDocument();
  });

  it("lists the linked case by title, ID, type and reason", async () => {
    list.mockResolvedValue([LINK]);
    render(<CaseLinks caseId={CASE_ID} closed={false} canWrite />);

    const row = (await screen.findByText("Ministry portal exposure")).closest("tr") as HTMLElement;
    expect(within(row).getByText(OTHER_ID)).toBeInTheDocument();
    expect(within(row).getByText("Related")).toBeInTheDocument();
    expect(within(row).getByText("Same exposure reported twice")).toBeInTheDocument();
    expect(list).toHaveBeenCalledWith(CASE_ID);
  });

  it("names the main case of a duplicate and says when the link is shared", async () => {
    list.mockResolvedValue([
      { ...LINK, link_type: "duplicate", primary_side: "linked", primary_case_id: OTHER_ID, share_visibility: true },
      {
        ...LINK,
        linked_case_id: "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        linked_case_title: "Root",
        link_type: "root_sub",
        primary_side: "this",
        primary_case_id: CASE_ID,
      },
    ]);
    render(<CaseLinks caseId={CASE_ID} closed={false} canWrite />);

    const duplicateRow = (await screen.findByText("Ministry portal exposure")).closest("tr") as HTMLElement;
    expect(within(duplicateRow).getByText(/Duplicate, main case: linked case/)).toBeInTheDocument();
    expect(within(duplicateRow).getByText(`Main or root case ID: ${OTHER_ID}`)).toBeInTheDocument();
    expect(within(duplicateRow).getByText("Users of either case see both")).toBeInTheDocument();
    const rootRow = screen.getByText("Root").closest("tr") as HTMLElement;
    expect(within(rootRow).getByText(/Root and sub-case, root: this case/)).toBeInTheDocument();
    expect(within(rootRow).getByText(`Main or root case ID: ${CASE_ID}`)).toBeInTheDocument();
  });

  it("says the main case ID is not available when the caller cannot see that case", async () => {
    list.mockResolvedValue([{ ...HIDDEN, link_type: "root_sub", primary_side: "linked", primary_case_id: null }]);
    render(<CaseLinks caseId={CASE_ID} closed={false} canWrite />);

    const [cell] = await screen.findAllByText("Not available to your account");
    const row = cell.closest("tr") as HTMLElement;
    expect(within(row).getByText("Main or root case ID: not available to your account")).toBeInTheDocument();
  });

  it("shows a link to a case the caller cannot see without an ID, a title or a remove button", async () => {
    list.mockResolvedValue([HIDDEN]);
    render(<CaseLinks caseId={CASE_ID} closed={false} canWrite />);

    // The title cell and the reason cell both say the case is not available.
    const [cell] = await screen.findAllByText("Not available to your account");
    const row = cell.closest("tr") as HTMLElement;
    expect(within(row).getAllByText("Not available to your account")).toHaveLength(2);
    expect(within(row).queryByRole("button", { name: /^Remove/ })).not.toBeInTheDocument();
    expect(within(row).getAllByText("-").length).toBeGreaterThan(0);
  });

  it("reads links without the add form or remove buttons when the caller cannot write", async () => {
    list.mockResolvedValue([LINK]);
    render(<CaseLinks caseId={CASE_ID} closed={false} canWrite={false} />);

    expect(await screen.findByText("Ministry portal exposure")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Remove/ })).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/Case ID to link/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Link case" })).not.toBeInTheDocument();
  });

  it("locks removal and hides the add form on a closed case", async () => {
    list.mockResolvedValue([LINK]);
    render(<CaseLinks caseId={CASE_ID} closed canWrite />);

    expect(await screen.findByRole("button", { name: /^Remove/ })).toBeDisabled();
    expect(screen.queryByRole("button", { name: "Link case" })).not.toBeInTheDocument();
  });

  it("shows an error when the links cannot be loaded", async () => {
    list.mockRejectedValue(new ApiError(404, "case not found"));
    render(<CaseLinks caseId={CASE_ID} closed={false} canWrite />);

    expect(await screen.findByText("No case with that ID is available to your account.")).toBeInTheDocument();
  });

  it("refuses an ID that is not a UUID without sending a request", async () => {
    const user = userEvent.setup();
    list.mockResolvedValue([]);
    render(<CaseLinks caseId={CASE_ID} closed={false} canWrite />);

    await typeLinkedCase(user, "not-an-id");
    await typeReason(user, "Same incident");
    await user.click(screen.getByRole("button", { name: "Link case" }));

    expect(await screen.findByText(/Enter the case ID as a UUID/)).toBeInTheDocument();
    expect(screen.getByLabelText(/^Case ID to link/)).toHaveAttribute("aria-invalid", "true");
    expect(add).not.toHaveBeenCalled();
  });

  it("requires a reason and refuses one of more than 500 characters without sending a request", async () => {
    const user = userEvent.setup();
    list.mockResolvedValue([]);
    render(<CaseLinks caseId={CASE_ID} closed={false} canWrite />);

    await typeLinkedCase(user, OTHER_ID);
    await user.click(screen.getByRole("button", { name: "Link case" }));
    expect(await screen.findByText("Enter a short reason for the link.")).toBeInTheDocument();
    expect(screen.getByLabelText(/^Link reason/)).toHaveAttribute("aria-invalid", "true");

    await typeReason(user, "x".repeat(501));
    await user.click(screen.getByRole("button", { name: "Link case" }));
    expect(await screen.findByText("The reason can be at most 500 characters.")).toBeInTheDocument();
    expect(add).not.toHaveBeenCalled();
  });

  it("links a related case with the trimmed reason, the checkbox off by default, and reloads", async () => {
    const user = userEvent.setup();
    list.mockResolvedValueOnce([]).mockResolvedValueOnce([LINK]);
    add.mockResolvedValue(LINK);
    render(<CaseLinks caseId={CASE_ID} closed={false} canWrite />);

    await typeLinkedCase(user, `  ${OTHER_ID}  `);
    await typeReason(user, "  Same incident  ");
    expect(screen.getByRole("checkbox", { name: /Users who can see one of the two cases/ })).not.toBeChecked();
    await user.click(screen.getByRole("button", { name: "Link case" }));

    await waitFor(() =>
      expect(add).toHaveBeenCalledWith(CASE_ID, {
        linked_case_id: OTHER_ID,
        link_type: "related",
        reason: "Same incident",
        share_visibility: false,
      }),
    );
    expect(toast.success).toHaveBeenCalledWith("Cases linked. Linking changes no task, deadline or status.");
    expect(await screen.findByText("Ministry portal exposure")).toBeInTheDocument();
    expect(list).toHaveBeenCalledTimes(2);
  });

  it("sends the visibility checkbox when it is ticked", async () => {
    const user = userEvent.setup();
    list.mockResolvedValue([]);
    add.mockResolvedValue(LINK);
    render(<CaseLinks caseId={CASE_ID} closed={false} canWrite />);

    await typeLinkedCase(user, OTHER_ID);
    await typeReason(user, "Same incident");
    await user.click(screen.getByRole("checkbox", { name: /Users who can see one of the two cases/ }));
    expect(screen.getByRole("checkbox", { name: /Users who can see one of the two cases/ })).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Link case" }));

    await waitFor(() => expect(add).toHaveBeenCalledWith(CASE_ID, expect.objectContaining({ share_visibility: true })));
  });

  it("asks for the main case of a duplicate and refuses to send without one", async () => {
    const user = userEvent.setup();
    list.mockResolvedValue([]);
    render(<CaseLinks caseId={CASE_ID} closed={false} canWrite />);

    await chooseType(user, /^Link type/, "Duplicate");
    await typeLinkedCase(user, OTHER_ID);
    await typeReason(user, "Same incident");
    await user.click(screen.getByRole("button", { name: "Link case" }));

    expect(await screen.findByText(/Choose which case is the main case/)).toBeInTheDocument();
    expect(add).not.toHaveBeenCalled();
  });

  it("links a duplicate with this case as the main case and says the duplicate is closed", async () => {
    const user = userEvent.setup();
    list.mockResolvedValue([]);
    add.mockResolvedValue({ ...LINK, link_type: "duplicate", primary_side: "this" });
    render(<CaseLinks caseId={CASE_ID} closed={false} canWrite />);

    await chooseType(user, /^Link type/, "Duplicate");
    await user.click(screen.getByRole("combobox", { name: /^Main case/ }));
    await user.click(await screen.findByRole("option", { name: "This case" }));
    await typeLinkedCase(user, OTHER_ID);
    await typeReason(user, "Same incident");
    await user.click(screen.getByRole("button", { name: "Link case" }));

    await waitFor(() =>
      expect(add).toHaveBeenCalledWith(CASE_ID, {
        linked_case_id: OTHER_ID,
        link_type: "duplicate",
        reason: "Same incident",
        share_visibility: false,
        primary_case_id: CASE_ID,
      }),
    );
    expect(toast.success).toHaveBeenCalledWith(
      "Duplicate linked. The duplicate case is now closed. No task was moved to the main case.",
    );
  });

  it("sends the case ID above as the root of a root and sub-case link", async () => {
    const user = userEvent.setup();
    list.mockResolvedValue([]);
    add.mockResolvedValue({ ...LINK, link_type: "root_sub", primary_side: "linked" });
    render(<CaseLinks caseId={CASE_ID} closed={false} canWrite />);

    await chooseType(user, /^Link type/, "Root and sub-case");
    await user.click(screen.getByRole("combobox", { name: /^Root case/ }));
    await user.click(await screen.findByRole("option", { name: "The case ID above" }));
    await typeLinkedCase(user, OTHER_ID);
    await typeReason(user, "Analysis of the root");
    await user.click(screen.getByRole("button", { name: "Link case" }));

    await waitFor(() =>
      expect(add).toHaveBeenCalledWith(
        CASE_ID,
        expect.objectContaining({ link_type: "root_sub", primary_case_id: OTHER_ID }),
      ),
    );
  });

  it("shows the message for a refused link", async () => {
    const user = userEvent.setup();
    list.mockResolvedValue([]);
    add.mockRejectedValue(new ApiError(409, "cases are already linked"));
    render(<CaseLinks caseId={CASE_ID} closed={false} canWrite />);

    await typeLinkedCase(user, OTHER_ID);
    await typeReason(user, "Same incident");
    await user.click(screen.getByRole("button", { name: "Link case" }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("These two cases are already linked."));
  });

  it("shows the blockers when a duplicate cannot be closed yet", async () => {
    const user = userEvent.setup();
    list.mockResolvedValue([]);
    add.mockRejectedValue(
      new ApiError(409, "duplicate case cannot be closed: deadline task affected_customers is open"),
    );
    render(<CaseLinks caseId={CASE_ID} closed={false} canWrite />);

    await chooseType(user, /^Link type/, "Duplicate");
    await user.click(screen.getByRole("combobox", { name: /^Main case/ }));
    await user.click(await screen.findByRole("option", { name: "This case" }));
    await typeLinkedCase(user, OTHER_ID);
    await typeReason(user, "Same incident");
    await user.click(screen.getByRole("button", { name: "Link case" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "The duplicate cannot be closed yet: deadline task affected_customers is open. No link was made.",
      ),
    );
  });

  it("removes a link by the linked case ID and reloads", async () => {
    const user = userEvent.setup();
    list.mockResolvedValueOnce([LINK]).mockResolvedValueOnce([]);
    remove.mockResolvedValue(undefined);
    render(<CaseLinks caseId={CASE_ID} closed={false} canWrite />);

    await user.click(await screen.findByRole("button", { name: /^Remove/ }));

    await waitFor(() => expect(remove).toHaveBeenCalledWith(CASE_ID, OTHER_ID));
    expect(toast.success).toHaveBeenCalledWith("The link was removed. No task, deadline or status changed.");
    expect(await screen.findByText("No case is linked to this case yet.")).toBeInTheDocument();
  });

  it("shows the message when a removal is refused", async () => {
    const user = userEvent.setup();
    list.mockResolvedValue([LINK]);
    remove.mockRejectedValue(new ApiError(404, "link not found"));
    render(<CaseLinks caseId={CASE_ID} closed={false} canWrite />);

    await user.click(await screen.findByRole("button", { name: /^Remove/ }));

    await waitFor(() => expect(toast.error).toHaveBeenCalledWith("These two cases are not linked."));
  });
});

it("fills the case ID from a case found by reference", async () => {
  list.mockResolvedValue([]);
  findCases.mockResolvedValue({
    items: [{ id: OTHER_ID, reference: "SAFE-2026-0042", title: "Ministry portal exposure", status: "open" } as CaseSummary],
    next_cursor: null,
  });
  render(<CaseLinks caseId={CASE_ID} closed={false} canWrite />);

  const user = userEvent.setup();
  await user.type(await screen.findByLabelText(/find the case by reference/i), "0042");
  await user.click(screen.getByRole("button", { name: "Find" }));
  await user.click(await screen.findByRole("button", { name: "Use this case" }));

  expect(findCases).toHaveBeenCalledWith(10, undefined, "0042");
  expect(screen.getByLabelText(/case id to link/i)).toHaveValue(OTHER_ID);
  expect(screen.getByText("Selected case: SAFE-2026-0042")).toBeInTheDocument();
});
