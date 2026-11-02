import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import { ApiError, ImpactScores, getImpactScores, updateImpactScores } from "../../api";
import { CaseRiskScore } from "./CaseRiskScore";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, getImpactScores: vi.fn(), updateImpactScores: vi.fn() };
});

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const get = vi.mocked(getImpactScores);
const save = vi.mocked(updateImpactScores);

const ASSESSED = { risk_score: 3.7, impact_c: 4, impact_b: 3, impact_a: 4 };
const UNASSESSED = { risk_score: null, impact_c: null, impact_b: null, impact_a: null };

const STORED: ImpactScores = {
  case_id: "case-1",
  version: 2,
  c: 4,
  b: 3,
  a: 4,
  risk_score: 3.7,
  created_by: "user-1",
  created_at: "2026-11-02T09:00:00Z",
};

const BASE_PROPS = {
  caseId: "case-1",
  risk: ASSESSED,
  closed: false,
  canWrite: true,
  onSaved: () => {},
};

beforeEach(() => {
  get.mockReset().mockResolvedValue(STORED);
  save.mockReset();
  vi.mocked(toast.success).mockClear();
});

// The text of the value shown under a label, for example "4" or "Not set".
function valueOf(label: string): string | null {
  return screen.getByText(label).nextElementSibling?.textContent ?? null;
}

async function openEditor(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: "Edit impact values" }));
  const dialog = await screen.findByRole("dialog");
  // The form loads the current version first. Wait until the selects hold it.
  await within(dialog).findByRole("combobox", { name: "C – Confidentiality" });
  return dialog;
}

async function choose(user: ReturnType<typeof userEvent.setup>, label: string, option: string) {
  await user.click(screen.getByRole("combobox", { name: label }));
  await user.click(await screen.findByRole("option", { name: option }));
}

describe("CaseRiskScore", () => {
  it("shows the score and the three values under their labels", () => {
    render(<CaseRiskScore {...BASE_PROPS} canWrite={false} />);

    expect(screen.getByRole("heading", { name: "Risk score" })).toBeInTheDocument();
    expect(screen.getByText("3.7 (High)")).toBeInTheDocument();
    expect(valueOf("C – Confidentiality")).toBe("4");
    expect(valueOf("B – Integrity")).toBe("3");
    expect(valueOf("A – Availability")).toBe("4");
  });

  it("shows Not assessed and Not set while a value is missing", () => {
    render(<CaseRiskScore {...BASE_PROPS} risk={UNASSESSED} canWrite={false} />);

    expect(screen.getByText("Not assessed")).toBeInTheDocument();
    expect(valueOf("C – Confidentiality")).toBe("Not set");
    expect(valueOf("B – Integrity")).toBe("Not set");
    expect(valueOf("A – Availability")).toBe("Not set");
  });

  it("offers the edit button only to a caller who may change the values of an open case", () => {
    const { unmount } = render(<CaseRiskScore {...BASE_PROPS} />);
    expect(screen.getByRole("button", { name: "Edit impact values" })).toBeInTheDocument();
    unmount();

    const reader = render(<CaseRiskScore {...BASE_PROPS} canWrite={false} />);
    expect(screen.queryByRole("button", { name: "Edit impact values" })).not.toBeInTheDocument();
    expect(screen.getByText("The impact values of this case. Your account cannot change them.")).toBeInTheDocument();
    reader.unmount();

    render(<CaseRiskScore {...BASE_PROPS} closed />);
    expect(screen.queryByRole("button", { name: "Edit impact values" })).not.toBeInTheDocument();
    expect(screen.getByText("This case is closed, so its impact values cannot be changed.")).toBeInTheDocument();
  });

  it("loads the current version and fills the three selects with the stored values", async () => {
    const user = userEvent.setup();
    render(<CaseRiskScore {...BASE_PROPS} />);

    const dialog = await openEditor(user);

    expect(get).toHaveBeenCalledWith("case-1");
    expect(within(dialog).getByText(/Editing started from version 2/)).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "C – Confidentiality" })).toHaveTextContent("4");
    expect(screen.getByRole("combobox", { name: "B – Integrity" })).toHaveTextContent("3");
    expect(screen.getByRole("combobox", { name: "A – Availability" })).toHaveTextContent("4");
  });

  it("saves the chosen values against the version it read and reports the new version", async () => {
    const user = userEvent.setup();
    const onSaved = vi.fn();
    save.mockResolvedValue({ ...STORED, version: 3, c: 5, b: null, a: 2, risk_score: 3.5 });
    render(<CaseRiskScore {...BASE_PROPS} onSaved={onSaved} />);

    const dialog = await openEditor(user);
    await choose(user, "C – Confidentiality", "5");
    await choose(user, "B – Integrity", "Not set");
    await choose(user, "A – Availability", "2");
    await user.click(within(dialog).getByRole("button", { name: "Save new version" }));

    await waitFor(() =>
      expect(save).toHaveBeenCalledWith("case-1", { expected_version: 2, c: 5, b: null, a: 2 }),
    );
    expect(onSaved).toHaveBeenCalledWith(expect.objectContaining({ version: 3, risk_score: 3.5 }));
    expect(toast.success).toHaveBeenCalledWith("Impact values saved as version 3.");
  });

  it("refuses a stale save, names the current version, and keeps the chosen values", async () => {
    const user = userEvent.setup();
    const onSaved = vi.fn();
    save.mockRejectedValue(new ApiError(409, "impact_version_conflict", 3));
    get.mockResolvedValueOnce(STORED).mockResolvedValueOnce({ ...STORED, version: 3 });
    render(<CaseRiskScore {...BASE_PROPS} onSaved={onSaved} />);

    const dialog = await openEditor(user);
    await choose(user, "C – Confidentiality", "5");
    await user.click(within(dialog).getByRole("button", { name: "Save new version" }));

    expect(await within(dialog).findByText(/The impact values were changed by someone else/)).toBeInTheDocument();
    expect(within(dialog).getByText(/The current version is 3/)).toBeInTheDocument();
    expect(onSaved).not.toHaveBeenCalled();
    expect(screen.getByRole("combobox", { name: "C – Confidentiality" })).toHaveTextContent("5");
  });
});
