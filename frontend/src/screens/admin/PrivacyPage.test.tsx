import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { getPrivacyInventory, PrivacyInventoryItem } from "../../api";
import { PrivacyPage } from "./PrivacyPage";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, getPrivacyInventory: vi.fn() };
});

vi.mock("../../system-timezone", () => ({
  useSystemTimezone: () => ({ timezone: "UTC", effectiveFrom: null, error: null, setCurrent: () => {}, reload: () => {} }),
}));

const inventory = vi.mocked(getPrivacyInventory);

function item(index: number): PrivacyInventoryItem {
  return {
    table: `table_${index}`,
    description: `Table ${index}`,
    category: "test",
    contains_personal_data: false,
    personal_fields: [],
    purpose: "test",
    storage: "test",
    retention: "test",
    retention_defined: true,
    security: "test",
    deletion_mode: "soft",
    rows: { total: 0, active: 0, soft_deleted: 0 },
    oldest_created_at: null,
    newest_created_at: null,
  } as PrivacyInventoryItem;
}

beforeEach(() => {
  inventory.mockReset().mockResolvedValue({
    generated_at: "2026-11-01T10:00:00Z",
    items: Array.from({ length: 7 }, (_, index) => item(index + 1)),
  } as Awaited<ReturnType<typeof getPrivacyInventory>>);
});

describe("PrivacyPage pagination", () => {
  it("shows five table cards per page and pages through the rest", async () => {
    const user = userEvent.setup();
    render(<PrivacyPage />);

    expect(await screen.findByText("Showing 1–5 of 7")).toBeInTheDocument();
    expect(screen.getByText("table_1")).toBeInTheDocument();
    expect(screen.getByText("table_5")).toBeInTheDocument();
    expect(screen.queryByText("table_6")).not.toBeInTheDocument();

    const previous = screen.getByRole("button", { name: "Previous" });
    const next = screen.getByRole("button", { name: "Next" });
    expect(previous).toBeDisabled();

    await user.click(next);
    expect(screen.getByText("Showing 6–7 of 7")).toBeInTheDocument();
    expect(screen.getByText("table_6")).toBeInTheDocument();
    expect(screen.queryByText("table_1")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Next" })).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "Previous" }));
    expect(screen.getByRole("button", { name: "Previous" })).toBeDisabled();
  });
});
