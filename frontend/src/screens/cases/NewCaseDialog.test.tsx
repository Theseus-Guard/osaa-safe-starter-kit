import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CaseDetail, createCase } from "../../api";
import { NewCaseDialog } from "./NewCaseDialog";

// The system timezone is a per-test value. The dialog reads it on open.
const system = vi.hoisted(() => ({ timezone: "Europe/Istanbul" as string | null }));

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, createCase: vi.fn() };
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

const create = vi.mocked(createCase);

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
  create.mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("NewCaseDialog", () => {
  it("pre-fills the awareness time in the system timezone", () => {
    renderDialog();

    expect(screen.getByLabelText("Awareness date and time")).toHaveValue("2026-11-01");
    expect(screen.getByLabelText("Awareness time")).toHaveValue("09:00");
    expect(screen.getByText(/Times are in Europe\/Istanbul \(UTC\+03:00\)/)).toBeInTheDocument();
  });

  it("rejects an awareness time in the future", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderDialog();

    await user.type(screen.getByLabelText("Title"), "Example incident");
    fireEvent.change(screen.getByLabelText("Awareness time"), { target: { value: "10:00" } });
    await user.click(screen.getByRole("button", { name: "Create case" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("The awareness time cannot be in the future.");
    expect(create).not.toHaveBeenCalled();
  });

  it("submits a valid past awareness time as UTC and closes the dialog", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const created = { id: "case-1", reference: "CASE-1", title: "Example incident" } as CaseDetail;
    create.mockResolvedValue(created);
    const { onOpenChange, onCreated } = renderDialog();

    await user.type(screen.getByLabelText("Title"), "Example incident");
    fireEvent.change(screen.getByLabelText("Awareness time"), { target: { value: "08:30" } });
    await user.click(screen.getByRole("button", { name: "Create case" }));

    expect(create).toHaveBeenCalledWith({ title: "Example incident", awareness_time: "2026-11-01T05:30:00.000Z" });
    expect(onCreated).toHaveBeenCalledWith(created);
    expect(onOpenChange).toHaveBeenCalledWith(false);
  });

  it("asks for a title before sending", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderDialog();

    fireEvent.change(screen.getByLabelText("Awareness time"), { target: { value: "08:30" } });
    await user.click(screen.getByRole("button", { name: "Create case" }));

    expect(create).not.toHaveBeenCalled();
    // The title is required, so the browser blocks the submit before the custom check runs.
    expect(screen.getByLabelText("Title")).toBeInvalid();
  });

  it("explains a local time that does not exist because of a DST change", async () => {
    system.timezone = "America/New_York";
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    renderDialog();

    await user.type(screen.getByLabelText("Title"), "Example incident");
    fireEvent.change(screen.getByLabelText("Awareness date and time"), { target: { value: "2026-03-08" } });
    fireEvent.change(screen.getByLabelText("Awareness time"), { target: { value: "02:30" } });
    await user.click(screen.getByRole("button", { name: "Create case" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("does not exist in America/New_York");
    expect(create).not.toHaveBeenCalled();
  });

  it("shows the server message when the case cannot be created", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    const { ApiError } = await vi.importActual<typeof import("../../api")>("../../api");
    create.mockRejectedValue(new ApiError(400, "system timezone is not configured"));
    renderDialog();

    await user.type(screen.getByLabelText("Title"), "Example incident");
    fireEvent.change(screen.getByLabelText("Awareness time"), { target: { value: "08:30" } });
    await user.click(screen.getByRole("button", { name: "Create case" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("The system timezone is not set yet.");
  });
});
