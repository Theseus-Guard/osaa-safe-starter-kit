import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import {
  getAuditSummary,
  getDeadlineSummary,
  getMetricSeries,
  getOutboxSummary,
  getOverviewSummary,
  getUsersSummary,
  type Me,
  type OverviewSummary,
} from "../api";
import { Overview } from "./Overview";
import { axisInterval, formatChartDate, NO_DATA_MESSAGE } from "./overview/parts";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return {
    ...actual,
    getOverviewSummary: vi.fn(),
    getMetricSeries: vi.fn(),
    getDeadlineSummary: vi.fn(),
    getAuditSummary: vi.fn(),
    getOutboxSummary: vi.fn(),
    getUsersSummary: vi.fn(),
  };
});

// Recharts sizes its charts with ResizeObserver, which jsdom does not provide.
beforeAll(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
});

// The permission names each role holds by default (backend/src/safe_kit/rbac.py).
const PERMISSIONS: Record<string, string[]> = {
  reporter: ["cases:submit", "cases:read_own"],
  triager: ["cases:read_assigned", "cases:read_all", "cases:triage", "cases:assign", "cases:close", "evidence:read"],
  investigator: ["cases:read_assigned", "cases:assignable", "evidence:read", "evidence:write"],
  reviewer: ["cases:read_assigned", "cases:assignable", "evidence:read", "reviews:write"],
  disclosure_approver: ["disclosures:approve"],
  auditor: ["audit:read"],
  reporting_owner: ["cases:read_all", "reporter_contact:read"],
  admin: ["users:manage", "users:reset_password", "roles:manage", "privacy:read"],
};

function profileFor(role: string): Me {
  return {
    id: "11111111-1111-1111-1111-111111111111",
    username: `${role}-user`,
    role,
    password_change_required: false,
    timezone_setup_required: false,
    permissions: PERMISSIONS[role],
  } as unknown as Me;
}

const SUMMARY: OverviewSummary = {
  days: 30,
  timezone: "Europe/Istanbul",
  own_cases: { open_cases: 2, opened_in_period: 1, deadlines_due_72h: 1, deadlines_overdue: 0 },
  case_activity: { open_cases: 7, opened_in_period: 3 },
  users: { pending_password_change: 1 },
  ops: { failed_outbox_jobs: 0, status: "ok", heartbeat_age_seconds: 4 },
};

function series(metric: string, counts: number[]) {
  return {
    metric,
    days: counts.length,
    timezone: "Europe/Istanbul",
    points: counts.map((count, index) => ({ date: `2026-10-${String(index + 1).padStart(2, "0")}`, count })),
  };
}

beforeEach(() => {
  vi.mocked(getOverviewSummary).mockReset().mockResolvedValue(SUMMARY);
  vi.mocked(getMetricSeries).mockReset().mockImplementation(async (metric) => series(metric, [0, 2, 1]));
  vi.mocked(getDeadlineSummary).mockReset().mockResolvedValue({
    items: [{ kind: "weekly_update", open: 1, overdue: 0, done: 2, not_applicable: 0 }],
  });
  vi.mocked(getAuditSummary).mockReset().mockResolvedValue({
    days: 30,
    timezone: "Europe/Istanbul",
    actions: ["auth.login_failed", "other"],
    points: [{ date: "2026-10-01", counts: { "auth.login_failed": 2, other: 1 } }],
  });
  vi.mocked(getOutboxSummary).mockReset().mockResolvedValue({
    counts: { pending: 0, leased: 0, done: 5, failed: 0 },
    oldest_pending_age_seconds: null,
  });
  vi.mocked(getUsersSummary).mockReset().mockResolvedValue({
    total: 3,
    pending_password_change: 1,
    roles: [
      { role: "admin", active: 1, inactive: 0 },
      { role: "reporter", active: 1, inactive: 1 },
    ],
  });
});

function renderFor(role: string) {
  return render(<Overview profile={profileFor(role)} notice={null} />);
}

const CHART_TITLES = {
  cases: "Cases over time",
  deadlines: "Deadlines in my cases",
  audit: "Audit actions",
  outbox: "Outbox health",
  logins: "Login activity",
  users: "Accounts by role",
};

async function settle() {
  await waitFor(() => expect(screen.queryAllByText("Loading…")).toHaveLength(0));
}

describe("Overview layout by role", () => {
  it.each(["reporter", "triager", "investigator", "reviewer"])(
    "shows only the own case cards and the deadline chart to %s",
    async (role) => {
      renderFor(role);
      await settle();

      expect(screen.getByText("My open cases")).toBeInTheDocument();
      expect(screen.getByText("My deadlines due in 72 hours")).toBeInTheDocument();
      expect(screen.getByText("My overdue deadlines")).toBeInTheDocument();
      expect(screen.queryByText("Open cases (all)")).toBeNull();
      expect(screen.queryByText("Pending password changes")).toBeNull();

      expect(screen.getByText(CHART_TITLES.deadlines)).toBeInTheDocument();
      expect(screen.queryByText(CHART_TITLES.cases)).toBeNull();
      expect(screen.queryByText(CHART_TITLES.audit)).toBeNull();
      expect(screen.queryByText(CHART_TITLES.outbox)).toBeNull();
      expect(screen.queryByText(CHART_TITLES.logins)).toBeNull();
      expect(screen.queryByText(CHART_TITLES.users)).toBeNull();

      // A denied section sends no request.
      expect(getMetricSeries).not.toHaveBeenCalled();
      expect(getAuditSummary).not.toHaveBeenCalled();
      expect(getOutboxSummary).not.toHaveBeenCalled();
      expect(getUsersSummary).not.toHaveBeenCalled();
    },
  );

  it("shows the operations cards and charts to auditor, and no user or login data", async () => {
    renderFor("auditor");
    await settle();

    expect(screen.getByText("Open cases (all)")).toBeInTheDocument();
    expect(screen.getByText("Failed background jobs")).toBeInTheDocument();
    expect(screen.getByText("Worker heartbeat")).toBeInTheDocument();
    expect(screen.queryByText("My open cases")).toBeNull();
    expect(screen.queryByText("Pending password changes")).toBeNull();

    expect(screen.getByText(CHART_TITLES.cases)).toBeInTheDocument();
    expect(screen.getByText(CHART_TITLES.audit)).toBeInTheDocument();
    expect(screen.getByText(CHART_TITLES.outbox)).toBeInTheDocument();
    expect(screen.queryByText(CHART_TITLES.deadlines)).toBeNull();
    expect(screen.queryByText(CHART_TITLES.logins)).toBeNull();
    expect(screen.queryByText(CHART_TITLES.users)).toBeNull();

    expect(getDeadlineSummary).not.toHaveBeenCalled();
    expect(getUsersSummary).not.toHaveBeenCalled();
    // Login metrics are admin only, so only the case metric is requested.
    expect(getMetricSeries).toHaveBeenCalledWith("cases_created", 30);
    expect(getMetricSeries).not.toHaveBeenCalledWith("logins_failed", expect.anything());
  });

  it("shows every section to admin", async () => {
    renderFor("admin");
    await settle();

    expect(screen.getByText("Open cases (all)")).toBeInTheDocument();
    expect(screen.getByText("Pending password changes")).toBeInTheDocument();
    expect(screen.queryByText("My open cases")).toBeNull();

    // Admin holds no case permission, so the own-case deadline chart stays hidden.
    for (const title of [CHART_TITLES.cases, CHART_TITLES.audit, CHART_TITLES.outbox, CHART_TITLES.logins, CHART_TITLES.users]) {
      expect(screen.getByText(title)).toBeInTheDocument();
    }
    expect(screen.queryByText(CHART_TITLES.deadlines)).toBeNull();
    expect(getDeadlineSummary).not.toHaveBeenCalled();
    expect(getMetricSeries).toHaveBeenCalledWith("logins_success", 30);
    expect(getMetricSeries).toHaveBeenCalledWith("logins_failed", 30);
  });

  it.each(["disclosure_approver", "reporting_owner"])(
    "shows no chart and no section cards to %s",
    async (role) => {
      renderFor(role);
      await settle();

      expect(screen.getByText("Your role has no overview sections.")).toBeInTheDocument();
      for (const title of Object.values(CHART_TITLES)) {
        expect(screen.queryByText(title)).toBeNull();
      }
      expect(getMetricSeries).not.toHaveBeenCalled();
    },
  );
});

describe("Overview range selector", () => {
  it("starts at 30 days and reloads the summary and series for 7 days", async () => {
    const user = userEvent.setup();
    renderFor("auditor");
    await settle();

    const group = screen.getByRole("group", { name: "Date range" });
    expect(within(group).getByRole("button", { name: "30 days" })).toHaveAttribute("aria-pressed", "true");
    expect(within(group).getByRole("button", { name: "7 days" })).toHaveAttribute("aria-pressed", "false");

    await user.click(within(group).getByRole("button", { name: "7 days" }));

    await waitFor(() => expect(getOverviewSummary).toHaveBeenLastCalledWith(7));
    expect(getMetricSeries).toHaveBeenLastCalledWith("cases_created", 7);
    expect(getAuditSummary).toHaveBeenLastCalledWith(7);
    expect(within(group).getByRole("button", { name: "7 days" })).toHaveAttribute("aria-pressed", "true");
  });
});

// The Chart / Table toggle of one card.
function viewToggle(title: string) {
  return within(screen.getByRole("group", { name: `${title} view` }));
}

describe("Overview chart views", () => {
  it("shows the chart first, and the table only after Table is chosen for that card", async () => {
    const user = userEvent.setup();
    renderFor("auditor");
    await settle();

    expect(screen.queryByRole("table", { name: "Cases created per day" })).toBeNull();
    expect(viewToggle(CHART_TITLES.cases).getByRole("button", { name: "Chart" })).toHaveAttribute("aria-pressed", "true");

    await user.click(viewToggle(CHART_TITLES.cases).getByRole("button", { name: "Table" }));

    expect(screen.getByRole("table", { name: "Cases created per day" })).toBeInTheDocument();
    expect(viewToggle(CHART_TITLES.cases).getByRole("button", { name: "Table" })).toHaveAttribute("aria-pressed", "true");
    // Another card keeps its own view.
    expect(screen.queryByRole("table", { name: "Audit actions per day" })).toBeNull();
  });

  it("shows at most eight table rows until Show all is pressed", async () => {
    const user = userEvent.setup();
    vi.mocked(getMetricSeries).mockImplementation(async (metric) =>
      series(metric, Array.from({ length: 30 }, (_, index) => index)),
    );
    renderFor("auditor");
    await settle();

    await user.click(viewToggle(CHART_TITLES.cases).getByRole("button", { name: "Table" }));
    const table = screen.getByRole("table", { name: "Cases created per day" });
    expect(within(table).getAllByRole("row")).toHaveLength(9);

    await user.click(screen.getByRole("button", { name: "Show all" }));
    expect(within(table).getAllByRole("row")).toHaveLength(31);
    expect(screen.getByRole("button", { name: "Show fewer" })).toBeInTheDocument();
  });

  it("formats chart dates as day and short month, and keeps the date labels few", () => {
    expect(formatChartDate("2026-10-15")).toBe("15 Oct");
    expect(formatChartDate("2026-01-01")).toBe("1 Jan");
    for (const days of [7, 30, 90]) {
      // Labels are drawn at every (interval + 1)th point, so this is how many a chart shows.
      expect(Math.ceil(days / (axisInterval(days) + 1))).toBeLessThanOrEqual(6);
    }
  });
});

describe("Overview states", () => {

  it("shows the no-data message for an empty period and never a healthy state", async () => {
    vi.mocked(getMetricSeries).mockImplementation(async (metric) => series(metric, [0, 0, 0]));
    vi.mocked(getOutboxSummary).mockResolvedValue({
      counts: { pending: 0, leased: 0, done: 0, failed: 0 },
      oldest_pending_age_seconds: null,
    });
    renderFor("auditor");
    await settle();

    expect(screen.getAllByText(NO_DATA_MESSAGE).length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByText(/healthy/i)).toBeNull();
  });

  it("shows loading in the stat cards while the summary is pending", async () => {
    vi.mocked(getOverviewSummary).mockReturnValue(new Promise(() => {}));
    renderFor("triager");

    expect(screen.getAllByText("Loading…").length).toBeGreaterThanOrEqual(3);
  });

  it("shows the error and an unavailable card when the summary fails", async () => {
    vi.mocked(getOverviewSummary).mockRejectedValue(new TypeError("network"));
    renderFor("triager");

    await waitFor(() => expect(screen.getByText("The server could not be reached.")).toBeInTheDocument());
    expect(screen.getAllByText("Unavailable").length).toBeGreaterThanOrEqual(1);
  });

  it("marks a stale worker as stale, not as running", async () => {
    vi.mocked(getOverviewSummary).mockResolvedValue({
      ...SUMMARY,
      ops: { failed_outbox_jobs: 2, status: "stale", heartbeat_age_seconds: 300 },
    });
    renderFor("auditor");
    await settle();

    expect(screen.getByText("Stale")).toBeInTheDocument();
    expect(screen.getByText("Last heartbeat 5 min ago.")).toBeInTheDocument();
  });
});
