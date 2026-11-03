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
  approvals: null,
  weekly_updates: null,
};

const APPROVER_SUMMARY: OverviewSummary = {
  days: 30,
  timezone: "Europe/Istanbul",
  own_cases: null,
  case_activity: null,
  users: null,
  ops: null,
  approvals: {
    pending: 3,
    oldest_pending_age_days: 12,
    by_template: [
      { template: "confidential_rapid_alert", pending: 2, oldest_pending_age_days: 12 },
      { template: "de_identified_member_advisory", pending: 1, oldest_pending_age_days: 4 },
      { template: "redacted_public_safety_report", pending: 0, oldest_pending_age_days: null },
    ],
  },
  weekly_updates: null,
};

const WEEKLY_SUMMARY: OverviewSummary = {
  ...SUMMARY,
  own_cases: { open_cases: 2, opened_in_period: 1, deadlines_due_72h: 0, deadlines_overdue: 0 },
  weekly_updates: {
    open_significant_risk_cases: 2,
    latest_update_date: "2026-11-01",
    by_status: [
      { status: "open", cases_with_significant_risk: 2, cases_with_weekly_update: 2, latest_update_date: "2026-11-01" },
      { status: "closed", cases_with_significant_risk: 1, cases_with_weekly_update: 0, latest_update_date: null },
    ],
  },
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
  myCases: "My cases over time",
  myContributions: "My contributions over time",
  deadlines: "Deadlines in my cases",
  approvals: "Pending approvals",
  weekly: "Weekly updates",
  audit: "Audit actions",
  outbox: "Outbox health",
  logins: "Login activity",
  users: "Accounts by role",
};

// The metric names requested so far, in call order.
function requestedMetrics(): string[] {
  return vi.mocked(getMetricSeries).mock.calls.map(([metric]) => metric);
}

async function settle() {
  await waitFor(() => expect(screen.queryAllByText("Loading…")).toHaveLength(0));
}

describe("Overview layout by role", () => {
  it.each(["reporter", "triager", "investigator", "reviewer"])(
    "shows only the own case cards and charts to %s",
    async (role) => {
      renderFor(role);
      await settle();

      expect(screen.getByText("My open cases")).toBeInTheDocument();
      expect(screen.getByText("My deadlines due in 72 hours")).toBeInTheDocument();
      expect(screen.getByText("My overdue deadlines")).toBeInTheDocument();
      expect(screen.queryByText("Open cases (all)")).toBeNull();
      expect(screen.queryByText("Pending password changes")).toBeNull();

      expect(screen.getByText(CHART_TITLES.myCases)).toBeInTheDocument();
      expect(screen.getByText(CHART_TITLES.myContributions)).toBeInTheDocument();
      expect(screen.getByText(CHART_TITLES.deadlines)).toBeInTheDocument();
      expect(screen.queryByText(CHART_TITLES.cases)).toBeNull();
      expect(screen.queryByText(CHART_TITLES.audit)).toBeNull();
      expect(screen.queryByText(CHART_TITLES.outbox)).toBeNull();
      expect(screen.queryByText(CHART_TITLES.logins)).toBeNull();
      expect(screen.queryByText(CHART_TITLES.users)).toBeNull();

      // Only the caller's own series are requested. A denied section sends no request.
      expect(requestedMetrics().length).toBeGreaterThan(0);
      expect(requestedMetrics().every((metric) => metric.startsWith("my_"))).toBe(true);
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
    expect(requestedMetrics().some((metric) => metric.startsWith("my_"))).toBe(false);
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

  it("shows no chart and no section cards to reporting_owner", async () => {
    renderFor("reporting_owner");
    await settle();

    expect(screen.getByText("Your role has no overview sections.")).toBeInTheDocument();
    for (const title of Object.values(CHART_TITLES)) {
      expect(screen.queryByText(title)).toBeNull();
    }
    expect(getMetricSeries).not.toHaveBeenCalled();
  });
});

describe("Overview own-case panels by role", () => {
  it.each(["reporter", "triager", "investigator", "reviewer"])("shows the own-case panels to %s", async (role) => {
    renderFor(role);
    await settle();

    expect(screen.getByText(CHART_TITLES.myCases)).toBeInTheDocument();
    expect(screen.getByText(CHART_TITLES.myContributions)).toBeInTheDocument();
    expect(screen.getByText(CHART_TITLES.deadlines)).toBeInTheDocument();
  });

  it.each(["reporter", "triager"])("gives %s no weekly update panel", async (role) => {
    renderFor(role);
    await settle();

    expect(screen.queryByText(CHART_TITLES.weekly)).toBeNull();
    expect(screen.queryByText("Significant risks still open")).toBeNull();
  });

  it.each(["investigator", "reviewer"])("gives %s the weekly update panel", async (role) => {
    vi.mocked(getOverviewSummary).mockResolvedValue(WEEKLY_SUMMARY);
    renderFor(role);
    await settle();

    expect(screen.getByText(CHART_TITLES.weekly)).toBeInTheDocument();
    expect(screen.getByText("Significant risks still open")).toBeInTheDocument();
  });

  it.each([
    "reporter",
    "triager",
    "investigator",
    "reviewer",
    "auditor",
    "admin",
    "reporting_owner",
  ])("never shows the approval panel to %s", async (role) => {
    renderFor(role);
    await settle();

    expect(screen.queryByText(CHART_TITLES.approvals)).toBeNull();
    expect(screen.queryByText("Versions awaiting approval")).toBeNull();
  });

  it("shows the approval panel and its counts to the disclosure approver only", async () => {
    vi.mocked(getOverviewSummary).mockResolvedValue(APPROVER_SUMMARY);
    renderFor("disclosure_approver");
    await settle();

    expect(screen.getByText(CHART_TITLES.approvals)).toBeInTheDocument();
    expect(screen.getByText("Versions awaiting approval")).toBeInTheDocument();
    expect(screen.getByText("Oldest waiting version")).toBeInTheDocument();
    // The stat card and the table both show the oldest age.
    expect(screen.getAllByText("12 days").length).toBeGreaterThanOrEqual(1);
    expect(screen.queryByText(CHART_TITLES.myCases)).toBeNull();
    expect(screen.queryByText(CHART_TITLES.deadlines)).toBeNull();
    // The approver has no case data and asks for no series.
    expect(getDeadlineSummary).not.toHaveBeenCalled();
    expect(getMetricSeries).not.toHaveBeenCalled();
  });

  it("shows the approval counts per template as a table with no chart toggle", async () => {
    vi.mocked(getOverviewSummary).mockResolvedValue(APPROVER_SUMMARY);
    renderFor("disclosure_approver");
    await settle();

    const table = screen.getByRole("table", { name: "Pending approvals by template" });
    expect(within(table).getByText("Confidential rapid alert")).toBeInTheDocument();
    expect(within(table).getByText("12 days")).toBeInTheDocument();
    expect(within(table).getByText("None")).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: `${CHART_TITLES.approvals} view` })).toBeNull();
  });

  it("shows the no-data message when nothing waits for approval", async () => {
    vi.mocked(getOverviewSummary).mockResolvedValue({
      ...APPROVER_SUMMARY,
      approvals: { pending: 0, oldest_pending_age_days: null, by_template: APPROVER_SUMMARY.approvals!.by_template.map((item) => ({ ...item, pending: 0, oldest_pending_age_days: null })) },
    });
    renderFor("disclosure_approver");
    await settle();

    expect(screen.getByText("Nothing is waiting.")).toBeInTheDocument();
    expect(screen.getByText(NO_DATA_MESSAGE)).toBeInTheDocument();
  });

  it("shows the weekly stat with the latest update date and the status table", async () => {
    vi.mocked(getOverviewSummary).mockResolvedValue(WEEKLY_SUMMARY);
    renderFor("investigator");
    await settle();

    expect(screen.getByText("Latest weekly update on 1 Nov.")).toBeInTheDocument();
    const table = screen.getByRole("table", { name: "Weekly updates by case status" });
    expect(within(table).getByText("Open")).toBeInTheDocument();
    expect(within(table).getByText("1 Nov")).toBeInTheDocument();
    expect(within(table).getByText("None")).toBeInTheDocument();
  });

  it("says no weekly update exists yet when the case has none", async () => {
    vi.mocked(getOverviewSummary).mockResolvedValue({
      ...WEEKLY_SUMMARY,
      weekly_updates: {
        open_significant_risk_cases: 0,
        latest_update_date: null,
        by_status: [
          { status: "open", cases_with_significant_risk: 0, cases_with_weekly_update: 0, latest_update_date: null },
          { status: "closed", cases_with_significant_risk: 0, cases_with_weekly_update: 0, latest_update_date: null },
        ],
      },
    });
    renderFor("reviewer");
    await settle();

    expect(screen.getByText("No weekly update yet.")).toBeInTheDocument();
    expect(screen.getByText(NO_DATA_MESSAGE)).toBeInTheDocument();
  });

  it("shows the no-data message on the own-case charts when the period is empty", async () => {
    vi.mocked(getMetricSeries).mockImplementation(async (metric) => series(metric, [0, 0, 0]));
    renderFor("triager");
    await settle();

    expect(screen.getAllByText(NO_DATA_MESSAGE).length).toBeGreaterThanOrEqual(2);
  });

  it("requests the own series for the selected range", async () => {
    const user = userEvent.setup();
    renderFor("reporter");
    await settle();

    expect(getMetricSeries).toHaveBeenCalledWith("my_cases_open", 30);
    expect(getMetricSeries).toHaveBeenCalledWith("my_cases_closed", 30);
    expect(getMetricSeries).toHaveBeenCalledWith("my_actions_reviews", 30);

    const group = screen.getByRole("group", { name: "Date range" });
    await user.click(within(group).getByRole("button", { name: "7 days" }));

    await waitFor(() => expect(getMetricSeries).toHaveBeenCalledWith("my_actions_cases_closed", 7));
    expect(getMetricSeries).toHaveBeenCalledWith("my_cases_closed", 7);
  });

  it("shows the own-case series as a table after Table is chosen", async () => {
    const user = userEvent.setup();
    renderFor("reporter");
    await settle();

    await user.click(viewToggle(CHART_TITLES.myCases).getByRole("button", { name: "Table" }));

    expect(screen.getByRole("table", { name: "My cases per day" })).toBeInTheDocument();
    expect(viewToggle(CHART_TITLES.myContributions).getByRole("button", { name: "Chart" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
  });
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
