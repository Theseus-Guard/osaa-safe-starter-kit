import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CaseDetail, createCase, createReport } from "../../api";
import { NewCaseDialog } from "./NewCaseDialog";

// The system timezone is a per-test value. The dialog reads it on open.
const system = vi.hoisted(() => ({ timezone: "Europe/Istanbul" as string | null }));

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, createCase: vi.fn(), createReport: vi.fn() };
});

vi.mock("../../system-timezone", () => ({
  useSystemTimezone: () => ({
    timezone: system.timezone,
    effectiveFrom: null,
    error: null,
    setCurrent: () => {},
    reload: () => {},
  }),
}));

const report = vi.mocked(createReport);
const legacyCreate = vi.mocked(createCase);

// Istanbul is UTC+03:00 with no DST. The clock is fixed at 2026-11-01 06:00 UTC, which is 09:00 in Istanbul.
const NOW = new Date("2026-11-01T06:00:00Z");

function renderDialog() {
  const onOpenChange = vi.fn();
  const onCreated = vi.fn();
  render(<NewCaseDialog open onOpenChange={onOpenChange} onCreated={onCreated} />);
  return { onOpenChange, onCreated };
}

beforeEach(() => {
  // Only Date is faked, so user-event timers keep running.
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(NOW);
  system.timezone = "Europe/Istanbul";
  report.mockReset();
  legacyCreate.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("NewCaseDialog", () => {
  it("pre-fills the awareness time in the system timezone", () => {
    renderDialog();

    expect(screen.getByLabelText("Date")).toHaveValue("2026-11-01");
    expect(screen.getByLabelText("Time")).toHaveValue("09:00");
    expect(screen.getByText(/Times are in Europe\/Istanbul \(UTC\+03:00\)/)).toBeInTheDocument();
  });

  it("explains the optional contact field", () => {
    renderDialog();

    expect(screen.getByLabelText("Contact for follow-up")).toHaveAttribute("maxLength", "200");
    expect(
      screen.getByText("Optional. Visible only to the independent reporting owner. Leave blank to stay anonymous."),
    ).toBeInTheDocument();
  });

  it("rejects an awareness time in the future", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderDialog();

    await user.type(screen.getByLabelText(/^Title/), "Example incident");
    fireEvent.change(screen.getByLabelText("Time"), { target: { value: "10:00" } });
    await user.click(screen.getByRole("button", { name: "Create case" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("The awareness time cannot be in the future.");
    expect(report).not.toHaveBeenCalled();
  });

  it("submits a valid past awareness time as UTC to the reports endpoint and closes the dialog", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const created = { id: "case-1", reference: "CASE-1", title: "Example incident" } as CaseDetail;
    report.mockResolvedValue(created);
    const { onOpenChange, onCreated } = renderDialog();

    await user.type(screen.getByLabelText(/^Title/), "Example incident");
    fireEvent.change(screen.getByLabelText("Time"), { target: { value: "08:30" } });
    await user.click(screen.getByRole("button", { name: "Create case" }));

    expect(report).toHaveBeenCalledWith({
      title: "Example incident",
      awareness_time: "2026-11-01T05:30:00.000Z",
      contact: null,
    });
    expect(legacyCreate).not.toHaveBeenCalled();
    expect(onCreated).toHaveBeenCalledWith(created);
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("sends the contact trimmed", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    report.mockResolvedValue({ id: "case-1" } as CaseDetail);
    renderDialog();

    await user.type(screen.getByLabelText(/^Title/), "Example incident");
    await user.type(screen.getByLabelText("Contact for follow-up"), "  reporter@example.com  ");
    fireEvent.change(screen.getByLabelText("Time"), { target: { value: "08:30" } });
    await user.click(screen.getByRole("button", { name: "Create case" }));

    expect(report).toHaveBeenCalledWith(expect.objectContaining({ contact: "reporter@example.com" }));
  });

  it("sends a blank contact as null", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    report.mockResolvedValue({ id: "case-1" } as CaseDetail);
    renderDialog();

    await user.type(screen.getByLabelText(/^Title/), "Example incident");
    await user.type(screen.getByLabelText("Contact for follow-up"), "   ");
    fireEvent.change(screen.getByLabelText("Time"), { target: { value: "08:30" } });
    await user.click(screen.getByRole("button", { name: "Create case" }));

    expect(report).toHaveBeenCalledWith(expect.objectContaining({ contact: null }));
  });

  it("asks for a title before sending", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderDialog();

    fireEvent.change(screen.getByLabelText("Time"), { target: { value: "08:30" } });
    await user.click(screen.getByRole("button", { name: "Create case" }));

    expect(report).not.toHaveBeenCalled();
    // The form has noValidate, so the message is the one the screen announces, linked to the title field.
    const title = screen.getByLabelText(/^Title/);
    expect(title).toBeInvalid();
    expect(title).toHaveAccessibleDescription("Enter a title for the case.");
    expect(await screen.findByRole("alert")).toHaveTextContent("Enter a title for the case.");
  });

  it("explains a local time that does not exist because of a DST change", async () => {
    system.timezone = "America/New_York";
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderDialog();

    await user.type(screen.getByLabelText(/^Title/), "Example incident");
    fireEvent.change(screen.getByLabelText("Date"), { target: { value: "2026-03-08" } });
    fireEvent.change(screen.getByLabelText("Time"), { target: { value: "02:30" } });
    await user.click(screen.getByRole("button", { name: "Create case" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("does not exist in America/New_York");
    expect(report).not.toHaveBeenCalled();
  });

  it("shows the server message when the case cannot be created", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const { ApiError } = await vi.importActual<typeof import("../../api")>("../../api");
    report.mockRejectedValue(new ApiError(400, "system timezone is not configured"));
    renderDialog();

    await user.type(screen.getByLabelText(/^Title/), "Example incident");
    fireEvent.change(screen.getByLabelText("Time"), { target: { value: "08:30" } });
    await user.click(screen.getByRole("button", { name: "Create case" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("The system timezone is not set yet.");
  });
});
