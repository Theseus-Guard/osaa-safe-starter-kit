import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CaseSummary, listCases } from "../../api";
import { CaseSearch } from "./CaseSearch";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, listCases: vi.fn() };
});

const list = vi.mocked(listCases);

const CASE_ID = "7d9c2f7e-1a2b-4c3d-8e9f-0a1b2c3d4e5f";

const CASE: CaseSummary = {
  id: CASE_ID,
  reference: "SAFE-2026-000001",
  title: "Phishing report",
  awareness_time: "2026-10-30T08:00:00Z",
  status: "open",
  created_at: "2026-10-30T08:05:00Z",
  next_deadline: null,
  updated_at: "2026-10-30T08:05:00Z",
  risk_score: null,
  impact_c: null,
  impact_b: null,
  impact_a: null,
};

function page(items: CaseSummary[]) {
  return { items, next_cursor: null };
}

beforeEach(() => {
  window.location.hash = "";
  list.mockReset().mockResolvedValue(page([]));
});

describe("CaseSearch", () => {
  it("sends one debounced request with the typed q", async () => {
    const user = userEvent.setup();
    render(<CaseSearch />);

    await user.type(screen.getByRole("searchbox", { name: "Search cases by ID" }), "SAFE-2026");

    await waitFor(() => expect(list).toHaveBeenCalledWith(8, undefined, "SAFE-2026", "open"));
    expect(list).toHaveBeenCalledTimes(1);
  });

  it("does not send a request for a blank query", async () => {
    const user = userEvent.setup();
    render(<CaseSearch />);

    await user.type(screen.getByRole("searchbox", { name: "Search cases by ID" }), "   ");

    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(list).not.toHaveBeenCalled();
    expect(screen.queryByText("No cases found")).not.toBeInTheDocument();
  });

  it("lists matching cases as links to the case detail page", async () => {
    list.mockResolvedValue(page([CASE]));
    const user = userEvent.setup();
    render(<CaseSearch />);

    await user.type(screen.getByRole("searchbox", { name: "Search cases by ID" }), "SAFE");

    const link = await screen.findByRole("link", { name: /SAFE-2026-000001/ });
    expect(link).toHaveAttribute("href", `#/cases/${CASE_ID}`);
    expect(screen.getByText("Phishing report")).toBeInTheDocument();
    expect(screen.getByText("open")).toBeInTheDocument();
    expect(screen.getByRole("note")).toHaveTextContent("Only open cases are searched. Closed cases are not shown.");
  });

  it("shows No cases found when nothing matches", async () => {
    const user = userEvent.setup();
    render(<CaseSearch />);

    await user.type(screen.getByRole("searchbox", { name: "Search cases by ID" }), "SAFE-9999");

    expect(await screen.findByText("No cases found")).toBeInTheDocument();
  });

  it("closes the list on Escape", async () => {
    list.mockResolvedValue(page([CASE]));
    const user = userEvent.setup();
    render(<CaseSearch />);
    const input = screen.getByRole("searchbox", { name: "Search cases by ID" });

    await user.type(input, "SAFE");
    await screen.findByRole("link", { name: /SAFE-2026-000001/ });
    await user.keyboard("{Escape}");

    expect(screen.queryByRole("link", { name: /SAFE-2026-000001/ })).not.toBeInTheDocument();
  });

  it("closes the list on an outside click", async () => {
    list.mockResolvedValue(page([CASE]));
    const user = userEvent.setup();
    render(
      <>
        <CaseSearch />
        <button type="button">Elsewhere</button>
      </>,
    );

    await user.type(screen.getByRole("searchbox", { name: "Search cases by ID" }), "SAFE");
    await screen.findByRole("link", { name: /SAFE-2026-000001/ });
    await user.click(screen.getByRole("button", { name: "Elsewhere" }));

    expect(screen.queryByRole("link", { name: /SAFE-2026-000001/ })).not.toBeInTheDocument();
  });

  it("opens the first result on Enter", async () => {
    list.mockResolvedValue(page([CASE]));
    const user = userEvent.setup();
    render(<CaseSearch />);

    await user.type(screen.getByRole("searchbox", { name: "Search cases by ID" }), "SAFE");
    await screen.findByRole("link", { name: /SAFE-2026-000001/ });
    await user.keyboard("{Enter}");

    expect(window.location.hash).toBe(`#/cases/${CASE_ID}`);
  });
});
