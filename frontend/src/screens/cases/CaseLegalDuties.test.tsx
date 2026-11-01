import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { LegalDuty, addLegalDuty, listLegalDuties, updateLegalDuty } from "../../api";
import { CaseLegalDuties } from "./CaseLegalDuties";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, addLegalDuty: vi.fn(), listLegalDuties: vi.fn(), updateLegalDuty: vi.fn() };
});

vi.mock("../../system-timezone", () => ({
  useSystemTimezone: () => ({ timezone: "UTC", effectiveFrom: null, error: null, setCurrent: () => {}, reload: () => {} }),
}));

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const add = vi.mocked(addLegalDuty);
const list = vi.mocked(listLegalDuties);
const update = vi.mocked(updateLegalDuty);

const DUTY: LegalDuty = {
  id: "duty-1",
  case_id: "case-1",
  kind: "statutory",
  description: "Notify the regulator",
  source_reference: "GDPR Art. 33",
  jurisdiction: "EU",
  due_at: "2026-11-03T06:00:00Z",
  status: "open",
  created_by: "user-1",
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
  update.mockReset();
  vi.mocked(toast.success).mockClear();
});

describe("CaseLegalDuties", () => {
  it("lists each duty with its source and due time, apart from the SAFE deadlines", async () => {
    list.mockResolvedValue([DUTY]);
    render(<CaseLegalDuties {...BASE_PROPS} />);

    expect(await screen.findByText("Notify the regulator")).toBeInTheDocument();
    expect(screen.getByText("Source: GDPR Art. 33")).toBeInTheDocument();
    expect(screen.getByText("Jurisdiction: EU")).toBeInTheDocument();
    expect(screen.getByText("Statutory")).toBeInTheDocument();
    expect(screen.getByText("Legal duties keep their own due times. They do not change the SAFE deadline tasks of the case.")).toBeInTheDocument();
  });

  it("shows the duty text as not shown for a caller without cases:read_all", async () => {
    list.mockResolvedValue([{ ...DUTY, description: null, source_reference: null, jurisdiction: null }]);
    render(<CaseLegalDuties {...BASE_PROPS} canReadFullView={false} />);

    expect((await screen.findAllByText("Not shown for your role.")).length).toBeGreaterThan(0);
    expect(screen.queryByText("Notify the regulator")).not.toBeInTheDocument();
  });

  it("offers no change or add control without cases:triage or on a closed case", async () => {
    list.mockResolvedValue([DUTY]);
    const { rerender } = render(<CaseLegalDuties {...BASE_PROPS} canWrite={false} />);
    expect(await screen.findByText("Notify the regulator")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Mark done" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add legal duty" })).not.toBeInTheDocument();

    rerender(<CaseLegalDuties {...BASE_PROPS} closed />);
    expect(screen.queryByRole("button", { name: "Mark done" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Add legal duty" })).not.toBeInTheDocument();
  });

  it("marks a duty done and shows the stored status", async () => {
    list.mockResolvedValue([DUTY]);
    update.mockResolvedValue({ ...DUTY, status: "done" });
    const user = userEvent.setup();
    render(<CaseLegalDuties {...BASE_PROPS} />);

    await user.click(await screen.findByRole("button", { name: "Mark done" }));

    expect(update).toHaveBeenCalledWith("case-1", "duty-1", { status: "done" });
    expect(await screen.findByRole("button", { name: "Set open" })).toBeInTheDocument();
    expect(toast.success).toHaveBeenCalledWith("Statutory duty is now done.");
  });

  it("adds a duty with the trimmed fields and an empty jurisdiction sent as null", async () => {
    add.mockResolvedValue(DUTY);
    const onRecorded = vi.fn();
    const user = userEvent.setup();
    render(<CaseLegalDuties {...BASE_PROPS} onRecorded={onRecorded} />);

    await user.click(await screen.findByRole("button", { name: "Add legal duty" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByLabelText("Kind"));
    await user.click(await screen.findByRole("option", { name: "Contractual" }));
    await user.type(within(dialog).getByLabelText("Duty"), "  Report the breach to the customer  ");
    await user.type(within(dialog).getByLabelText("Source"), "Clause 9.2");
    await user.click(within(dialog).getByRole("button", { name: "Add duty" }));

    await waitFor(() =>
      expect(add).toHaveBeenCalledWith("case-1", {
        kind: "contractual",
        description: "Report the breach to the customer",
        source_reference: "Clause 9.2",
        jurisdiction: null,
        due_at: expect.any(String),
      }),
    );
    await waitFor(() => expect(onRecorded).toHaveBeenCalledTimes(1));
  });

  it("asks for the kind before saving", async () => {
    const user = userEvent.setup();
    render(<CaseLegalDuties {...BASE_PROPS} />);

    await user.click(await screen.findByRole("button", { name: "Add legal duty" }));
    const dialog = await screen.findByRole("dialog");
    await user.click(within(dialog).getByRole("button", { name: "Add duty" }));

    expect(add).not.toHaveBeenCalled();
    expect(await within(dialog).findByRole("alert")).toHaveTextContent("Choose whether the duty is statutory or contractual.");
  });
});
