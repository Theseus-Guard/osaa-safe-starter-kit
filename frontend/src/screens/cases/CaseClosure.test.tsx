import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { ApiError, CaseDetail, closeCase } from "../../api";
import { CaseClosure } from "./CaseClosure";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, closeCase: vi.fn() };
});

vi.mock("../../system-timezone", () => ({
  useSystemTimezone: () => ({ timezone: "UTC", effectiveFrom: null, error: null, setCurrent: () => {}, reload: () => {} }),
}));

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const close = vi.mocked(closeCase);

beforeEach(() => {
  close.mockReset();
  vi.mocked(toast.success).mockClear();
});

describe("CaseClosure", () => {
  it("asks for a reason before closing", async () => {
    const user = userEvent.setup();
    render(
      <CaseClosure caseId="case-1" status="open" closedAt={null} closeReason={null} onChanged={() => {}} />,
    );

    await user.type(screen.getByLabelText("Reason"), "   ");
    await user.click(screen.getByRole("button", { name: "Close case" }));

    expect(close).not.toHaveBeenCalled();
    expect(await screen.findByRole("alert")).toHaveTextContent("Enter the reason for closing the case.");
  });

  it("closes the case with the trimmed reason and reports the result", async () => {
    const closed = { id: "case-1", status: "closed" } as CaseDetail;
    close.mockResolvedValue(closed);
    const onChanged = vi.fn();
    const user = userEvent.setup();
    render(<CaseClosure caseId="case-1" status="open" closedAt={null} closeReason={null} onChanged={onChanged} />);

    await user.type(screen.getByLabelText("Reason"), "  Incident contained  ");
    await user.click(screen.getByRole("button", { name: "Close case" }));

    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(closed));
    expect(close).toHaveBeenCalledWith("case-1", "Incident contained");
    expect(toast.success).toHaveBeenCalledWith("Case closed.");
  });

  it("lists what blocks the closure when the server refuses", async () => {
    close.mockRejectedValue(new ApiError(409, "case cannot be closed: no review is recorded"));
    const user = userEvent.setup();
    render(<CaseClosure caseId="case-1" status="open" closedAt={null} closeReason={null} onChanged={() => {}} />);

    await user.type(screen.getByLabelText("Reason"), "Done");
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
        onChanged={() => {}}
      />,
    );

    expect(screen.queryByRole("button", { name: "Close case" })).not.toBeInTheDocument();
    expect(screen.getByText("Incident contained")).toBeInTheDocument();
    expect(screen.getByText("Closed at")).toBeInTheDocument();
  });
});
