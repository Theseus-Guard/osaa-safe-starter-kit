import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiError,
  CatalogEntry,
  Recommendation,
  addCatalogVersion,
  getCatalogEntry,
  listCatalogEntries,
  listRecommendations,
  verifyCatalogEntry,
  withdrawCatalogEntry,
} from "../../api";
import { CatalogEntryPage } from "./CatalogEntryPage";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return {
    ...actual,
    getCatalogEntry: vi.fn(),
    addCatalogVersion: vi.fn(),
    withdrawCatalogEntry: vi.fn(),
    verifyCatalogEntry: vi.fn(),
    listCatalogEntries: vi.fn(),
    listRecommendations: vi.fn(),
  };
});

vi.mock("../../system-timezone", () => ({
  useSystemTimezone: () => ({ timezone: "UTC", effectiveFrom: null, error: null, setCurrent: () => {}, reload: () => {} }),
}));

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const getEntry = vi.mocked(getCatalogEntry);
const addVersion = vi.mocked(addCatalogVersion);
const withdraw = vi.mocked(withdrawCatalogEntry);
const verify = vi.mocked(verifyCatalogEntry);
const entries = vi.mocked(listCatalogEntries);
const recommendations = vi.mocked(listRecommendations);

const READER = ["catalog:read"];
const WRITER = ["catalog:read", "catalog:write"];
const RESTRICTED_READER = ["catalog:read", "catalog:write", "catalog:read_restricted"];
const RESTRICTED_LINK = "https://restricted.example.invalid/report/SECRET-1";

const FIRST = {
  version: 1,
  operation: "create" as const,
  status: "active",
  title: "Deny unscoped tool calls",
  body: "Calls outside the approved scope are refused.",
  reason: null,
  replacement_entry_id: null,
  created_by: "user-1",
  created_at: "2026-11-01T06:00:00Z",
  verification: null,
  verified: false,
};

const CHECKED = {
  version: 2,
  operation: "version" as const,
  status: "active",
  title: "Deny unscoped tool calls",
  body: "Calls outside the approved scope are refused.",
  reason: null,
  replacement_entry_id: null,
  created_by: "user-1",
  created_at: "2026-11-01T07:00:00Z",
  verification: { run_id: "run-1", subject: "gateway replay", method: "replay", result: "pass" as const, verified_at: "2026-11-01T07:00:00Z" },
  verified: true,
};

function entry(overrides: Partial<CatalogEntry> = {}, current = CHECKED): CatalogEntry {
  return {
    id: "entry-1",
    created_by: "user-1",
    created_at: "2026-11-01T06:00:00Z",
    status: "active",
    version: current.version,
    current,
    versions: [FIRST, current],
    ...overrides,
  };
}

const RECOMMENDATION: Recommendation = {
  id: "rec-1",
  failure_or_system: "tool gateway accepts unscoped calls",
  required_defence_outcome: "every call is checked",
  minimum_control: "deny by default",
  control_alternative: null,
  responsible: "platform owner",
  due_at: "2026-12-01T09:00:00Z",
  evidence_to_keep: "policy diff",
  created_by: "user-1",
  created_at: "2026-11-01T05:00:00Z",
};

beforeEach(() => {
  getEntry.mockReset();
  addVersion.mockReset();
  withdraw.mockReset();
  verify.mockReset();
  entries.mockReset();
  recommendations.mockReset();
  entries.mockResolvedValue({ items: [entry({ id: "entry-2" })], next_cursor: null });
  recommendations.mockResolvedValue({ items: [RECOMMENDATION], next_cursor: null });
});

describe("CatalogEntryPage", () => {
  it("shows the versions and the verification history of the entry", async () => {
    getEntry.mockResolvedValue(entry());
    render(<CatalogEntryPage entryId="entry-1" permissions={READER} />);

    expect(await screen.findByText("Verification history")).toBeInTheDocument();
    expect(screen.getByText("gateway replay")).toBeInTheDocument();
    const versionRow = screen.getByText("gateway replay").closest("tr") as HTMLElement;
    expect(within(versionRow).getByText("pass")).toBeInTheDocument();
    expect(screen.getByText("Yes", { selector: "dd" })).toBeInTheDocument();
  });

  it("shows the restricted reference only when the API returned it", async () => {
    getEntry.mockResolvedValue(
      entry({}, { ...CHECKED, restricted: { reference: RESTRICTED_LINK, digest: "ab".repeat(32) } }),
    );
    render(<CatalogEntryPage entryId="entry-1" permissions={RESTRICTED_READER} />);

    expect(await screen.findByText(RESTRICTED_LINK)).toBeInTheDocument();
  });

  it("does not show a restricted reference the caller was not sent", async () => {
    getEntry.mockResolvedValue(entry());
    render(<CatalogEntryPage entryId="entry-1" permissions={WRITER} />);

    await screen.findByText("Deny unscoped tool calls");
    expect(screen.queryByText("Restricted reference", { exact: true })).toBeNull();
    expect(screen.queryByText(RESTRICTED_LINK)).toBeNull();
  });

  it("gives a reader the history and no forms", async () => {
    getEntry.mockResolvedValue(entry());
    render(<CatalogEntryPage entryId="entry-1" permissions={READER} />);

    await screen.findByText("Version history");
    expect(screen.queryByRole("form", { name: "Save a new version" })).toBeNull();
    expect(screen.queryByRole("form", { name: "Record a verification run" })).toBeNull();
    expect(screen.queryByRole("form", { name: "Withdraw the entry" })).toBeNull();
  });

  it("offers the change forms to a writer on an open entry", async () => {
    getEntry.mockResolvedValue(entry());
    render(<CatalogEntryPage entryId="entry-1" permissions={WRITER} />);

    expect(await screen.findByRole("form", { name: "Save a new version" })).toBeInTheDocument();
    expect(screen.getByRole("form", { name: "Correct the entry" })).toBeInTheDocument();
    expect(screen.getByRole("form", { name: "Dispute the entry" })).toBeInTheDocument();
    expect(screen.getByRole("form", { name: "Withdraw the entry" })).toBeInTheDocument();
    expect(await screen.findByRole("form", { name: "Supersede the entry" })).toBeInTheDocument();
    expect(await screen.findByRole("form", { name: "Record a verification run" })).toBeInTheDocument();
  });

  it("explains that a withdrawn entry accepts no change and shows no forms", async () => {
    getEntry.mockResolvedValue(entry({ status: "withdrawn", current: { ...CHECKED, status: "withdrawn" } }));
    render(<CatalogEntryPage entryId="entry-1" permissions={WRITER} />);

    expect(await screen.findByText(/accepts no further changes/)).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Save a new version" })).toBeNull();
    expect(screen.queryByRole("form", { name: "Record a verification run" })).toBeNull();
  });

  it("saves a new version against the version the page shows, and reloads", async () => {
    const user = userEvent.setup();
    getEntry.mockResolvedValue(entry());
    addVersion.mockResolvedValue(entry({ version: 3 }));
    render(<CatalogEntryPage entryId="entry-1" permissions={WRITER} />);

    const form = await screen.findByRole("form", { name: "Save a new version" });
    const title = within(form).getByLabelText("Title");
    await user.clear(title);
    await user.type(title, "Deny unscoped calls");
    await user.click(within(form).getByRole("button", { name: "Save version" }));

    await waitFor(() =>
      expect(addVersion).toHaveBeenCalledWith("entry-1", 2, {
        title: "Deny unscoped calls",
        body: "Calls outside the approved scope are refused.",
        restricted_reference: null,
      }),
    );
    await waitFor(() => expect(getEntry).toHaveBeenCalledTimes(2));
  });

  it("shows the version conflict text and saves nothing else when the version is stale", async () => {
    const user = userEvent.setup();
    getEntry.mockResolvedValue(entry());
    addVersion.mockRejectedValue(new ApiError(409, "version_conflict", 3));
    render(<CatalogEntryPage entryId="entry-1" permissions={WRITER} />);

    const form = await screen.findByRole("form", { name: "Save a new version" });
    await user.click(within(form).getByRole("button", { name: "Save version" }));

    expect(
      await within(form).findByText("The item changed, so nothing was saved. Reload it, check the current version and save again."),
    ).toBeInTheDocument();
  });

  it("withdraws the entry with its reason", async () => {
    const user = userEvent.setup();
    getEntry.mockResolvedValue(entry());
    withdraw.mockResolvedValue(entry({ status: "withdrawn" }));
    render(<CatalogEntryPage entryId="entry-1" permissions={WRITER} />);

    const form = await screen.findByRole("form", { name: "Withdraw the entry" });
    await user.type(within(form).getByLabelText("Reason"), "retired");
    await user.click(within(form).getByRole("button", { name: "Withdraw entry" }));

    await waitFor(() => expect(withdraw).toHaveBeenCalledWith("entry-1", 2, "retired"));
  });

  it("records a failing verification run and says the entry is not verified", async () => {
    const user = userEvent.setup();
    getEntry.mockResolvedValue(entry());
    verify.mockResolvedValue(entry({ version: 3 }, { ...CHECKED, version: 3, verified: false }));
    render(<CatalogEntryPage entryId="entry-1" permissions={WRITER} />);

    const form = await screen.findByRole("form", { name: "Record a verification run" });
    await waitFor(() => expect(within(form).getByLabelText("Recommendation")).toHaveValue("rec-1"));
    await user.selectOptions(within(form).getByLabelText("Result"), "fail");
    await user.type(within(form).getByLabelText("Subject"), "gateway replay");
    await user.type(within(form).getByLabelText("Scope"), "staging");
    await user.type(within(form).getByLabelText("Assumptions"), "scoped tokens only");
    await user.type(within(form).getByLabelText("Evidence path"), "evidence/run-7");
    await user.type(within(form).getByLabelText("Failure behaviour"), "refused with 403");
    await user.type(within(form).getByLabelText("Error class"), "none observed");
    await user.type(within(form).getByLabelText("Uncertainty"), "one environment");
    await user.type(within(form).getByLabelText("Method"), "replay");
    await user.click(within(form).getByRole("button", { name: "Record verification" }));

    await waitFor(() => expect(verify).toHaveBeenCalledTimes(1));
    const [sent, recommendationId, body] = verify.mock.calls[0];
    expect(sent.id).toBe("entry-1");
    expect(recommendationId).toBe("rec-1");
    expect(body).toMatchObject({ result: "fail", subject: "gateway replay", method: "replay" });
  });

  it("refuses a verification with an empty field before any request", async () => {
    const user = userEvent.setup();
    getEntry.mockResolvedValue(entry());
    render(<CatalogEntryPage entryId="entry-1" permissions={WRITER} />);

    const form = await screen.findByRole("form", { name: "Record a verification run" });
    await user.click(within(form).getByRole("button", { name: "Record verification" }));

    expect(within(form).getByText("Enter subject.")).toBeInTheDocument();
    expect(verify).not.toHaveBeenCalled();
  });
});
