import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { ApiError, AwarenessEvent, addAwarenessEvent, listAwarenessEvents } from "../../api";
import { CaseAwarenessEvents } from "./CaseAwarenessEvents";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, addAwarenessEvent: vi.fn(), listAwarenessEvents: vi.fn() };
});

vi.mock("../../system-timezone", () => ({
  useSystemTimezone: () => ({ timezone: "UTC", effectiveFrom: null, error: null, setCurrent: () => {}, reload: () => {} }),
}));

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const add = vi.mocked(addAwarenessEvent);
const list = vi.mocked(listAwarenessEvents);

const EVENT: AwarenessEvent = {
  id: "event-1",
  case_id: "case-1",
  awareness_kind: "operator",
  event_kind: "incident",
  occurred_at: "2026-11-01T05:00:00Z",
  basis: "Alert from the log monitor",
  recorded_by: "user-1",
  created_at: "2026-11-01T06:00:00Z",
};

const BASE_PROPS = {
  caseId: "case-1",
  closed: false,
  canWrite: true,
  canReadFullView: true,
  onRecorded: () => {},
};

beforeEach(() => {
  add.mockReset();
  list.mockReset().mockResolvedValue([]);
  vi.mocked(toast.success).mockClear();
});

async function openDialog(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Add awareness event" }));
  return screen.findByRole("dialog");
}

async function pick(
  user: ReturnType<typeof userEvent.setup>,
  dialog: HTMLElement,
  label: string,
  option: string,
) {
  await user.click(within(dialog).getByLabelText(label));
  await user.click(await screen.findByRole("option", { name: option }));
}

describe("CaseAwarenessEvents", () => {
  it("lists each event with its kinds, time and basis for a caller with the full view", async () => {
    list.mockResolvedValue([EVENT]);
    render(<CaseAwarenessEvents {...BASE_PROPS} />);

    expect(await screen.findByText("Alert from the log monitor")).toBeInTheDocument();
    expect(list).toHaveBeenCalledWith("case-1");
    expect(screen.getByText("Operator")).toBeInTheDocument();
    expect(screen.getByText("Incident")).toBeInTheDocument();
  });

  it("shows the basis as not shown for a caller without cases:read_all", async () => {
    list.mockResolvedValue([{ ...EVENT, basis: null }]);
    render(<CaseAwarenessEvents {...BASE_PROPS} canReadFullView={false} />);

    expect(await screen.findByText("Not shown for your role.")).toBeInTheDocument();
    expect(screen.queryByText("Alert from the log monitor")).not.toBeInTheDocument();
  });

  it("shows an empty state when no event is recorded", async () => {
    render(<CaseAwarenessEvents {...BASE_PROPS} />);

    expect(await screen.findByText("No awareness event has been recorded for this case yet.")).toBeInTheDocument();
  });

  it("offers no add control without cases:triage or on a closed case", async () => {
    const { rerender } = render(<CaseAwarenessEvents {...BASE_PROPS} canWrite={false} />);
    expect(await screen.findByText("No awareness event has been recorded for this case yet.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add awareness event" })).not.toBeInTheDocument();

    rerender(<CaseAwarenessEvents {...BASE_PROPS} closed />);
    expect(screen.queryByRole("button", { name: "Add awareness event" })).not.toBeInTheDocument();
    expect(screen.getByText("This case is closed, so no awareness event can be recorded.")).toBeInTheDocument();
  });

  it("records an event with the trimmed basis, then reloads the list and reports the change", async () => {
    add.mockResolvedValue(EVENT);
    list.mockResolvedValueOnce([]).mockResolvedValueOnce([EVENT]);
    const onRecorded = vi.fn();
    const user = userEvent.setup();
    render(<CaseAwarenessEvents {...BASE_PROPS} onRecorded={onRecorded} />);

    const dialog = await openDialog(user);
    await pick(user, dialog, "Awareness through", "Operator");
    await pick(user, dialog, "Event", "Incident");
    await user.type(within(dialog).getByLabelText("Basis"), "  Alert from the log monitor  ");
    await user.click(within(dialog).getByRole("button", { name: "Add event" }));

    await waitFor(() =>
      expect(add).toHaveBeenCalledWith(
        "case-1",
        expect.objectContaining({
          awareness_kind: "operator",
          event_kind: "incident",
          basis: "Alert from the log monitor",
          occurred_at: expect.any(String),
        }),
      ),
    );
    await waitFor(() => expect(onRecorded).toHaveBeenCalledTimes(1));
    expect(toast.success).toHaveBeenCalledWith("Awareness event recorded.");
    expect(await screen.findByText("Alert from the log monitor")).toBeInTheDocument();
  });

  it("asks for the basis before saving", async () => {
    const user = userEvent.setup();
    render(<CaseAwarenessEvents {...BASE_PROPS} />);

    const dialog = await openDialog(user);
    await pick(user, dialog, "Awareness through", "Operator");
    await pick(user, dialog, "Event", "Incident");
    await user.click(within(dialog).getByRole("button", { name: "Add event" }));

    expect(add).not.toHaveBeenCalled();
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Enter the basis for this awareness event.");
  });

  it("shows the server message when the event is refused", async () => {
    add.mockRejectedValue(new ApiError(409, "case is closed"));
    const user = userEvent.setup();
    render(<CaseAwarenessEvents {...BASE_PROPS} />);

    const dialog = await openDialog(user);
    await pick(user, dialog, "Awareness through", "Operator");
    await pick(user, dialog, "Event", "Incident");
    await user.type(within(dialog).getByLabelText("Basis"), "Log alert");
    await user.click(within(dialog).getByRole("button", { name: "Add event" }));

    expect(await within(dialog).findByRole("alert")).toHaveTextContent("This case is closed.");
  });
});
