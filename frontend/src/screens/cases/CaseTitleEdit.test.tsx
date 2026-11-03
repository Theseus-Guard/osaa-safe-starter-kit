import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { ApiError, CaseDetail, updateCaseTitle } from "../../api";
import { CaseTitleEdit } from "./CaseTitleEdit";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, updateCaseTitle: vi.fn() };
});

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const save = vi.mocked(updateCaseTitle);

const SAVED: CaseDetail = {
  id: "case-1",
  reference: "CASE-1",
  title: "New title",
  awareness_time: "2026-11-01T05:00:00Z",
  status: "open",
  calendar_version: "2026.1",
  revision: 4,
  created_at: "2026-11-01T06:00:00Z",
  closed_at: null,
  close_reason: null,
  risk_score: null,
  impact_c: null,
  impact_b: null,
  impact_a: null,
  deadlines: [],
};

const BASE_PROPS = {
  caseId: "case-1",
  title: "Example incident",
  revision: 3,
  closed: false,
  canWrite: true,
  onSaved: vi.fn(),
  onReload: vi.fn(),
};

beforeEach(() => {
  save.mockReset();
  BASE_PROPS.onSaved.mockReset();
  BASE_PROPS.onReload.mockReset();
  vi.mocked(toast.success).mockClear();
});

async function openEditor(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Edit title" }));
  const dialog = await screen.findByRole("dialog");
  return dialog;
}

describe("CaseTitleEdit", () => {
  it("is offered only to a caller who may change the case record, and not once the case is closed", () => {
    const { unmount } = render(<CaseTitleEdit {...BASE_PROPS} canWrite={false} />);
    expect(screen.queryByRole("button", { name: "Edit title" })).not.toBeInTheDocument();
    unmount();

    const { unmount: unmountClosed } = render(<CaseTitleEdit {...BASE_PROPS} closed />);
    expect(screen.queryByRole("button", { name: "Edit title" })).not.toBeInTheDocument();
    unmountClosed();

    render(<CaseTitleEdit {...BASE_PROPS} />);
    expect(screen.getByRole("button", { name: "Edit title" })).toBeInTheDocument();
  });

  it("opens with the current title in a field that has a visible label", async () => {
    const user = userEvent.setup();
    render(<CaseTitleEdit {...BASE_PROPS} />);

    const dialog = await openEditor(user);
    const field = within(dialog).getByRole("textbox", { name: "Case title" });
    expect(field).toHaveValue("Example incident");
    expect(within(dialog).getByText("Case title")).toBeVisible();
  });

  it("saves the trimmed title with the revision the screen holds", async () => {
    const user = userEvent.setup();
    save.mockResolvedValue(SAVED);
    render(<CaseTitleEdit {...BASE_PROPS} />);

    const dialog = await openEditor(user);
    const field = within(dialog).getByRole("textbox", { name: "Case title" });
    await user.clear(field);
    await user.type(field, "  New title  ");
    await user.click(within(dialog).getByRole("button", { name: "Save title" }));

    expect(save).toHaveBeenCalledWith("case-1", "New title", 3);
    expect(BASE_PROPS.onSaved).toHaveBeenCalledWith(SAVED);
    expect(toast.success).toHaveBeenCalledWith("Case title saved.");
  });

  it("refuses a blank title before anything is sent", async () => {
    const user = userEvent.setup();
    render(<CaseTitleEdit {...BASE_PROPS} />);

    const dialog = await openEditor(user);
    const field = within(dialog).getByRole("textbox", { name: "Case title" });
    await user.clear(field);
    await user.type(field, "   ");
    await user.click(within(dialog).getByRole("button", { name: "Save title" }));

    expect(await within(dialog).findByText("The title must be 1 to 200 characters and not only spaces.")).toBeInTheDocument();
    expect(save).not.toHaveBeenCalled();
    expect(BASE_PROPS.onSaved).not.toHaveBeenCalled();
  });

  it("says the title changed on a 412 and offers a reload, without saving", async () => {
    const user = userEvent.setup();
    save.mockRejectedValue(new ApiError(412, "precondition_failed", null, 5));
    render(<CaseTitleEdit {...BASE_PROPS} />);

    const dialog = await openEditor(user);
    await user.click(within(dialog).getByRole("button", { name: "Save title" }));

    expect(await within(dialog).findByText(/The title changed after this case was loaded, so nothing was saved/)).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Save title" })).not.toBeInTheDocument();
    expect(BASE_PROPS.onSaved).not.toHaveBeenCalled();

    await user.click(within(dialog).getByRole("button", { name: "Reload case" }));
    expect(BASE_PROPS.onReload).toHaveBeenCalledTimes(1);
  });

  it("shows other failures as an error and keeps the form", async () => {
    const user = userEvent.setup();
    save.mockRejectedValue(new ApiError(409, "case is closed"));
    render(<CaseTitleEdit {...BASE_PROPS} />);

    const dialog = await openEditor(user);
    await user.click(within(dialog).getByRole("button", { name: "Save title" }));

    expect(await within(dialog).findByRole("button", { name: "Save title" })).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "Reload case" })).not.toBeInTheDocument();
    expect(BASE_PROPS.onSaved).not.toHaveBeenCalled();
  });
});
