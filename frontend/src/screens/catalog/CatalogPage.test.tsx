import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CatalogEntry, Recommendation, createCatalogEntry, createRecommendation, listCatalogEntries, listRecommendations } from "../../api";
import { CatalogPage } from "./CatalogPage";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return {
    ...actual,
    listCatalogEntries: vi.fn(),
    listRecommendations: vi.fn(),
    createCatalogEntry: vi.fn(),
    createRecommendation: vi.fn(),
  };
});

vi.mock("../../system-timezone", () => ({
  useSystemTimezone: () => ({ timezone: "UTC", effectiveFrom: null, error: null, setCurrent: () => {}, reload: () => {} }),
}));

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const entries = vi.mocked(listCatalogEntries);
const recommendations = vi.mocked(listRecommendations);
const createEntry = vi.mocked(createCatalogEntry);
const createRecommendationMock = vi.mocked(createRecommendation);

const READER = ["catalog:read"];
const WRITER = ["catalog:read", "catalog:write"];

const ENTRY: CatalogEntry = {
  id: "entry-1",
  created_by: "user-1",
  created_at: "2026-11-01T06:00:00Z",
  status: "active",
  version: 3,
  current: {
    version: 3,
    operation: "correction",
    status: "active",
    title: "Deny unscoped tool calls",
    body: "Calls outside the approved scope are refused.",
    reason: "wider scope named",
    replacement_entry_id: null,
    created_by: "user-1",
    created_at: "2026-11-01T06:00:00Z",
    verification: { run_id: "run-1", subject: "gateway", method: "replay", result: "pass", verified_at: "2026-11-01T07:00:00Z" },
    verified: true,
  },
};

const WITHDRAWN: CatalogEntry = {
  ...ENTRY,
  id: "entry-2",
  status: "withdrawn",
  version: 2,
  current: { ...ENTRY.current, title: "Retired rule", status: "withdrawn", verified: false, verification: null, version: 2 },
};

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
  entries.mockReset();
  recommendations.mockReset();
  createEntry.mockReset();
  createRecommendationMock.mockReset();
  entries.mockResolvedValue({ items: [ENTRY, WITHDRAWN], next_cursor: null });
  recommendations.mockResolvedValue({ items: [RECOMMENDATION], next_cursor: null });
});

describe("CatalogPage", () => {
  it("lists each entry with its status and version, and marks the verified one", async () => {
    render(<CatalogPage permissions={READER} />);

    const row = (await screen.findByText("Deny unscoped tool calls")).closest("tr") as HTMLElement;
    expect(within(row).getByText("active")).toBeInTheDocument();
    expect(within(row).getByText("3")).toBeInTheDocument();
    expect(within(row).getByText("Yes")).toBeInTheDocument();
    const withdrawnRow = screen.getByText("Retired rule").closest("tr") as HTMLElement;
    expect(within(withdrawnRow).getByText("withdrawn")).toBeInTheDocument();
    expect(within(withdrawnRow).getByText("No")).toBeInTheDocument();
  });

  it("links each entry title to its detail page", async () => {
    render(<CatalogPage permissions={READER} />);

    const link = await screen.findByRole("link", { name: "Deny unscoped tool calls" });
    expect(link).toHaveAttribute("href", "#/catalog/entry-1");
  });

  it("lists the recommendations, which carry no result", async () => {
    render(<CatalogPage permissions={READER} />);

    expect(await screen.findByText("tool gateway accepts unscoped calls")).toBeInTheDocument();
    expect(screen.getByText(/A recommendation has no result/)).toBeInTheDocument();
  });

  it("shows an empty state for an empty catalog", async () => {
    entries.mockResolvedValue({ items: [], next_cursor: null });
    recommendations.mockResolvedValue({ items: [], next_cursor: null });
    render(<CatalogPage permissions={READER} />);

    expect(await screen.findByText("No catalog entries yet.")).toBeInTheDocument();
    expect(screen.getByText("No recommendations yet.")).toBeInTheDocument();
  });

  it("explains the missing permission and loads nothing without catalog:read", () => {
    render(<CatalogPage permissions={["cases:read_all"]} />);

    expect(screen.getByText("You do not have permission to view the catalog.")).toBeInTheDocument();
    expect(entries).not.toHaveBeenCalled();
    expect(recommendations).not.toHaveBeenCalled();
  });

  it("hides the write forms from a reader", async () => {
    render(<CatalogPage permissions={READER} />);

    await screen.findByText("Deny unscoped tool calls");
    expect(screen.queryByRole("form", { name: "New catalog entry" })).toBeNull();
    expect(screen.queryByRole("form", { name: "New recommendation" })).toBeNull();
  });

  it("offers the entry and recommendation forms to a writer", async () => {
    render(<CatalogPage permissions={WRITER} />);

    expect(await screen.findByRole("form", { name: "New catalog entry" })).toBeInTheDocument();
    expect(screen.getByRole("form", { name: "New recommendation" })).toBeInTheDocument();
  });

  it("creates an entry and reloads the list", async () => {
    const user = userEvent.setup();
    createEntry.mockResolvedValue(ENTRY);
    render(<CatalogPage permissions={WRITER} />);

    await screen.findByText("Deny unscoped tool calls");
    const form = screen.getByRole("form", { name: "New catalog entry" });
    await user.type(within(form).getByLabelText("Title"), "New rule");
    await user.type(within(form).getByLabelText("Body"), "Refuse by default.");
    await user.click(within(form).getByRole("button", { name: "Create entry" }));

    await waitFor(() => expect(createEntry).toHaveBeenCalledWith({ title: "New rule", body: "Refuse by default.", restricted_reference: null }));
    await waitFor(() => expect(entries).toHaveBeenCalledTimes(2));
  });

  it("refuses an empty title before any request is sent", async () => {
    const user = userEvent.setup();
    render(<CatalogPage permissions={WRITER} />);

    await screen.findByText("Deny unscoped tool calls");
    const form = screen.getByRole("form", { name: "New catalog entry" });
    await user.click(within(form).getByRole("button", { name: "Create entry" }));

    expect(within(form).getByText("Enter a title.")).toBeInTheDocument();
    expect(createEntry).not.toHaveBeenCalled();
  });

  it("sends a recommendation's due time as an instant with its time zone", async () => {
    const user = userEvent.setup();
    createRecommendationMock.mockResolvedValue(RECOMMENDATION);
    render(<CatalogPage permissions={WRITER} />);

    await screen.findByText("Deny unscoped tool calls");
    const form = screen.getByRole("form", { name: "New recommendation" });
    await user.type(within(form).getByLabelText("Failure or system"), "gateway");
    await user.type(within(form).getByLabelText("Required defence outcome"), "every call checked");
    await user.type(within(form).getByLabelText("Minimum control"), "deny by default");
    await user.type(within(form).getByLabelText("Responsible"), "platform owner");
    await user.type(within(form).getByLabelText("Evidence to keep"), "policy diff");
    await user.type(within(form).getByLabelText("Due (your local time)"), "2026-12-01T09:00");
    await user.click(within(form).getByRole("button", { name: "Save recommendation" }));

    await waitFor(() => expect(createRecommendationMock).toHaveBeenCalledTimes(1));
    const sent = createRecommendationMock.mock.calls[0][0];
    expect(sent.due_at).toBe(new Date("2026-12-01T09:00").toISOString());
    expect(sent.control_alternative).toBeNull();
  });
});
