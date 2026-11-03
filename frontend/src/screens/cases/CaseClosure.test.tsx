import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { ApiError, CaseDetail, closeCase, reopenCase } from "../../api";
import { CaseClosure } from "./CaseClosure";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, closeCase: vi.fn(), reopenCase: vi.fn() };
});

vi.mock("../../system-timezone", () => ({
  useSystemTimezone: () => ({ timezone: "UTC", effectiveFrom: null, error: null, setCurrent: () => {}, reload: () => {} }),
}));

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const close = vi.mocked(closeCase);
const reopen = vi.mocked(reopenCase);

beforeEach(() => {
  close.mockReset();
  reopen.mockReset();
  vi.mocked(toast.success).mockClear();
});

describe("CaseClosure", () => {
  it("asks for a reason before closing", async () => {
    const user = userEvent.setup();
    render(
      <CaseClosure
        caseId="case-1"
        status="open"
        closedAt={null}
        closeReason={null}
        reopenReason={null}
        canClose
        onChanged={() => {}}
      />,
    );

    await user.type(screen.getByLabelText(/^Reason/), "   ");
    await user.click(screen.getByRole("button", { name: "Close case" }));

    expect(close).not.toHaveBeenCalled();
    expect(await screen.findByRole("alert")).toHaveTextContent("Enter the reason for closing the case.");
  });

  it("closes the case with the trimmed reason and reports the result", async () => {
    const closed = { id: "case-1", status: "closed" } as CaseDetail;
    close.mockResolvedValue(closed);
    const onChanged = vi.fn();
    const user = userEvent.setup();
    render(
      <CaseClosure
        caseId="case-1"
        status="open"
        closedAt={null}
        closeReason={null}
        reopenReason={null}
        canClose
        onChanged={onChanged}
      />,
    );

    await user.type(screen.getByLabelText(/^Reason/), "  Incident contained  ");
    await user.click(screen.getByRole("button", { name: "Close case" }));

    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(closed));
    expect(close).toHaveBeenCalledWith("case-1", "Incident contained");
    expect(toast.success).toHaveBeenCalledWith("Case closed.");
  });

  it("lists what blocks the closure when the server refuses", async () => {
    close.mockRejectedValue(new ApiError(409, "case cannot be closed: no review is recorded"));
    const user = userEvent.setup();
    render(
      <CaseClosure
        caseId="case-1"
        status="open"
        closedAt={null}
        closeReason={null}
        reopenReason={null}
        canClose
        onChanged={() => {}}
      />,
    );

    await user.type(screen.getByLabelText(/^Reason/), "Done");
    await user.click(screen.getByRole("button", { name: "Close case" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The case cannot be closed yet: no review is recorded.",
    );
  });

  it("shows the closure time and reason once the case is closed", () => {
    render(
      <CaseClosure
        caseId="case-1"
        status="closed"
        closedAt="2026-11-01T06:00:00Z"
        closeReason="Incident contained"
        reopenReason={null}
        canClose
        onChanged={() => {}}
      />,
    );

    expect(screen.queryByRole("button", { name: "Close case" })).not.toBeInTheDocument();
    expect(screen.getByText("Incident contained")).toBeInTheDocument();
    expect(screen.getByText("Closed at")).toBeInTheDocument();
    expect(screen.queryByText("Reopen reason")).not.toBeInTheDocument();
  });

  it("replaces the close form with a note when the caller cannot close", () => {
    render(
      <CaseClosure
        caseId="case-1"
        status="open"
        closedAt={null}
        closeReason={null}
        reopenReason={null}
        canClose={false}
        onChanged={() => {}}
      />,
    );

    expect(screen.getByText("Your account cannot close cases.")).toBeInTheDocument();
    expect(screen.queryByLabelText("Reason")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Close case" })).not.toBeInTheDocument();
  });

  it("offers reopen only on a closed case to a caller with cases:close", () => {
    const { unmount } = render(
      <CaseClosure
        caseId="case-1"
        status="closed"
        closedAt="2026-11-01T06:00:00Z"
        closeReason="Done"
        reopenReason={null}
        canClose
        onChanged={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: "Reopen case" })).toBeInTheDocument();
    unmount();

    render(
      <CaseClosure
        caseId="case-1"
        status="closed"
        closedAt="2026-11-01T06:00:00Z"
        closeReason="Done"
        reopenReason={null}
        canClose={false}
        onChanged={() => {}}
      />,
    );
    expect(screen.queryByRole("button", { name: "Reopen case" })).not.toBeInTheDocument();
  });

  it("asks for a reason before reopening", async () => {
    const user = userEvent.setup();
    render(
      <CaseClosure
        caseId="case-1"
        status="closed"
        closedAt="2026-11-01T06:00:00Z"
        closeReason="Done"
        reopenReason={null}
        canClose
        onChanged={() => {}}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Reopen case" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText(/^Reason/), "   ");
    await user.click(within(dialog).getByRole("button", { name: "Reopen case" }));

    expect(reopen).not.toHaveBeenCalled();
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Enter the reason for reopening the case.");
  });

  it("reopens the case with the trimmed reason and reports the result", async () => {
    const reopened = { id: "case-1", status: "open" } as CaseDetail;
    reopen.mockResolvedValue(reopened);
    const onChanged = vi.fn();
    const user = userEvent.setup();
    render(
      <CaseClosure
        caseId="case-1"
        status="closed"
        closedAt="2026-11-01T06:00:00Z"
        closeReason="Done"
        reopenReason={null}
        canClose
        onChanged={onChanged}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Reopen case" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText(/^Reason/), "  New evidence arrived  ");
    await user.click(within(dialog).getByRole("button", { name: "Reopen case" }));

    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(reopened));
    expect(reopen).toHaveBeenCalledWith("case-1", "New evidence arrived");
    expect(toast.success).toHaveBeenCalledWith("Case reopened.");
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  });

  it("lists the server refusal when the case cannot be reopened", async () => {
    reopen.mockRejectedValue(new ApiError(409, "case is not closed"));
    const user = userEvent.setup();
    render(
      <CaseClosure
        caseId="case-1"
        status="closed"
        closedAt="2026-11-01T06:00:00Z"
        closeReason="Done"
        reopenReason={null}
        canClose
        onChanged={() => {}}
      />,
    );

    await user.click(screen.getByRole("button", { name: "Reopen case" }));
    const dialog = await screen.findByRole("dialog");
    await user.type(within(dialog).getByLabelText(/^Reason/), "Reopen");
    await user.click(within(dialog).getByRole("button", { name: "Reopen case" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent(
      "This case is not closed, so it cannot be reopened.",
    );
  });

  it("shows the reason of the last reopen on a closed case", () => {
    render(
      <CaseClosure
        caseId="case-1"
        status="closed"
        closedAt="2026-11-01T06:00:00Z"
        closeReason="Done again"
        reopenReason="New evidence arrived"
        canClose
        onChanged={() => {}}
      />,
    );

    expect(screen.getByText("Reopen reason")).toBeInTheDocument();
    expect(screen.getByText("New evidence arrived")).toBeInTheDocument();
  });

  it("shows the reason of the last reopen on an open case", () => {
    render(
      <CaseClosure
        caseId="case-1"
        status="open"
        closedAt={null}
        closeReason={null}
        reopenReason="New evidence arrived"
        canClose
        onChanged={() => {}}
      />,
    );

    expect(screen.getByText("Reopen reason")).toBeInTheDocument();
    expect(screen.getByText("New evidence arrived")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Close case" })).toBeInTheDocument();
  });
});
