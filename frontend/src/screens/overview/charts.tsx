import { Bar, BarChart, CartesianGrid, Cell, Line, LineChart, XAxis, YAxis } from "recharts";
import {
  ChartContainer,
  ChartLegend,
  ChartLegendContent,
  ChartTooltip,
  ChartTooltipContent,
  type ChartConfig,
} from "@/components/ui/chart";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import {
  getAuditSummary,
  getDeadlineSummary,
  getMetricSeries,
  getOutboxSummary,
  getUsersSummary,
  type AuditSummary,
  type OverviewRange,
  type SeriesPoint,
} from "../../api";
import { ChartPanel, formatAge, formatDay } from "./parts";
import { useLoad } from "./useLoad";

// One row per x value. Keys are chart series keys; they never contain dots, which Recharts reads as a path.
interface Row {
  [key: string]: string | number;
}

interface Series {
  key: string;
  label: string;
  color: string;
}

// Charts use the fixed series order chart-1 to chart-5 (index.css). "Other" is always neutral.
const OTHER_COLOR = "var(--muted-foreground)";

function isAllZero(rows: Row[], keys: string[]): boolean {
  return rows.every((row) => keys.every((key) => row[key] === 0));
}

function configFor(series: Series[]): ChartConfig {
  return Object.fromEntries(series.map((item) => [item.key, { label: item.label, color: item.color }]));
}

function seriesRows(points: SeriesPoint[], key: string): Row[] {
  return points.map((point) => ({ date: point.date, [key]: point.count }));
}

function DailyLineChart({ rows, series }: { rows: Row[]; series: Series[] }) {
  return (
    <ChartContainer config={configFor(series)} className="h-64 w-full">
      <LineChart data={rows} accessibilityLayer>
        <CartesianGrid vertical={false} />
        <XAxis dataKey="date" tickFormatter={formatDay} tickLine={false} axisLine={false} minTickGap={16} />
        <YAxis allowDecimals={false} tickLine={false} axisLine={false} width={40} />
        <ChartTooltip content={<ChartTooltipContent />} />
        <ChartLegend content={<ChartLegendContent />} />
        {series.map((item) => (
          <Line
            key={item.key}
            dataKey={item.key}
            type="monotone"
            stroke={`var(--color-${item.key})`}
            strokeWidth={2}
            dot={false}
          />
        ))}
      </LineChart>
    </ChartContainer>
  );
}

function DailyStackedBars({ rows, series }: { rows: Row[]; series: Series[] }) {
  return (
    <ChartContainer config={configFor(series)} className="h-64 w-full">
      <BarChart data={rows} accessibilityLayer>
        <CartesianGrid vertical={false} />
        <XAxis dataKey="date" tickFormatter={formatDay} tickLine={false} axisLine={false} minTickGap={16} />
        <YAxis allowDecimals={false} tickLine={false} axisLine={false} width={40} />
        <ChartTooltip content={<ChartTooltipContent />} />
        <ChartLegend content={<ChartLegendContent />} />
        {series.map((item) => (
          <Bar key={item.key} dataKey={item.key} stackId="day" fill={`var(--color-${item.key})`} />
        ))}
      </BarChart>
    </ChartContainer>
  );
}

interface DataTableProps {
  label: string;
  headers: string[];
  rows: (string | number)[][];
}

// The table view of a chart: the same numbers, one row per x value. The first column is the label; the rest are numbers.
function DataTable({ label, headers, rows }: DataTableProps) {
  return (
    <Table aria-label={label}>
      <TableHeader>
        <TableRow>
          {headers.map((header, index) => (
            <TableHead key={header} className={index === 0 ? undefined : "text-right"}>
              {header}
            </TableHead>
          ))}
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((cells, rowIndex) => (
          <TableRow key={rowIndex}>
            {cells.map((cell, index) => (
              <TableCell key={index} className={index === 0 ? undefined : "text-right tabular-nums"}>
                {cell}
              </TableCell>
            ))}
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}

// Cases over time (auditor, admin). One line, one series.
const CASES_SERIES: Series[] = [{ key: "count", label: "Cases created", color: "var(--chart-1)" }];

export function CasesOverTimeChart({ days }: { days: OverviewRange }) {
  const state = useLoad(async () => seriesRows((await getMetricSeries("cases_created", days)).points, "count"), [
    days,
  ]);
  return (
    <ChartPanel
      title="Cases over time"
      description={`Cases created each day in the last ${days} days, in the system timezone.`}
      state={state}
      isEmpty={(rows) => isAllZero(rows, ["count"])}
      chart={(rows) => <DailyLineChart rows={rows} series={CASES_SERIES} />}
      table={(rows) => (
        <DataTable
          label="Cases created per day"
          headers={["Date", "Cases created"]}
          rows={rows.map((row) => [String(row.date), row.count])}
        />
      )}
    />
  );
}

// Login activity (admin only). Successes and failures, one line each.
const LOGIN_SERIES: Series[] = [
  { key: "success", label: "Successful logins", color: "var(--chart-1)" },
  { key: "failed", label: "Failed logins", color: "var(--chart-2)" },
];

export function LoginsChart({ days }: { days: OverviewRange }) {
  const state = useLoad(async () => {
    const [success, failed] = await Promise.all([
      getMetricSeries("logins_success", days),
      getMetricSeries("logins_failed", days),
    ]);
    return success.points.map((point, index) => ({
      date: point.date,
      success: point.count,
      failed: failed.points[index]?.count ?? 0,
    }));
  }, [days]);
  return (
    <ChartPanel
      title="Login activity"
      description={`Successful and failed logins each day in the last ${days} days.`}
      state={state}
      isEmpty={(rows) => isAllZero(rows, ["success", "failed"])}
      chart={(rows) => <DailyLineChart rows={rows} series={LOGIN_SERIES} />}
      table={(rows) => (
        <DataTable
          label="Logins per day"
          headers={["Date", "Successful", "Failed"]}
          rows={rows.map((row) => [String(row.date), row.success, row.failed])}
        />
      )}
    />
  );
}

const DEADLINE_LABELS: Record<string, string> = {
  affected_customers: "Affected customers",
  preliminary_safe_report: "Preliminary SAFE report",
  broader_advisory: "Broader advisory",
  preliminary_public_report: "Preliminary public report",
  remediation_status: "Remediation status",
  weekly_update: "Weekly update",
};

const DEADLINE_SERIES: Series[] = [
  { key: "open", label: "Open", color: "var(--chart-1)" },
  { key: "overdue", label: "Overdue", color: "var(--chart-2)" },
  { key: "done", label: "Done", color: "var(--chart-3)" },
];

// Deadline status per kind, for the caller's own cases (reporter, triager, investigator, reviewer).
export function DeadlinesChart() {
  const state = useLoad(async () => {
    const summary = await getDeadlineSummary();
    return summary.items.map((item) => ({
      label: DEADLINE_LABELS[item.kind] ?? item.kind,
      open: item.open,
      overdue: item.overdue,
      done: item.done,
      notApplicable: item.not_applicable,
    }));
  }, []);
  return (
    <ChartPanel
      title="Deadlines in my cases"
      description="Open, overdue and done deadline tasks per kind, across the cases you created or are assigned to."
      state={state}
      isEmpty={(rows) => isAllZero(rows, ["open", "overdue", "done", "notApplicable"])}
      chart={(rows) => (
        <ChartContainer config={configFor(DEADLINE_SERIES)} className="h-72 w-full">
          <BarChart data={rows} layout="vertical" accessibilityLayer>
            <CartesianGrid horizontal={false} />
            <XAxis type="number" allowDecimals={false} tickLine={false} axisLine={false} />
            <YAxis type="category" dataKey="label" width={170} tickLine={false} axisLine={false} />
            <ChartTooltip content={<ChartTooltipContent />} />
            <ChartLegend content={<ChartLegendContent />} />
            {DEADLINE_SERIES.map((item) => (
              <Bar key={item.key} dataKey={item.key} fill={`var(--color-${item.key})`} />
            ))}
          </BarChart>
        </ChartContainer>
      )}
      table={(rows) => (
        <DataTable
          label="Deadline tasks by kind"
          headers={["Deadline", "Open", "Overdue", "Done", "Not applicable"]}
          rows={rows.map((row) => [String(row.label), row.open, row.overdue, row.done, row.notApplicable])}
        />
      )}
    />
  );
}

// Audit actions per day (auditor, admin). At most five named actions; the rest share the "Other" bucket.
interface AuditView {
  rows: Row[];
  series: Series[];
}

function auditView(summary: AuditSummary): AuditView {
  const series = summary.actions.map((action, index) => ({
    key: `action${index}`,
    label: action === "other" ? "Other" : action,
    color: action === "other" ? OTHER_COLOR : `var(--chart-${index + 1})`,
  }));
  const rows = summary.points.map((point) => {
    const row: Row = { date: point.date };
    summary.actions.forEach((action, index) => {
      row[`action${index}`] = point.counts[action] ?? 0;
    });
    return row;
  });
  return { rows, series };
}

export function AuditChart({ days }: { days: OverviewRange }) {
  const state = useLoad(async () => auditView(await getAuditSummary(days)), [days]);
  return (
    <ChartPanel
      title="Audit actions"
      description={`Audit events per day by action, last ${days} days. Five actions at most; the rest are grouped as Other.`}
      state={state}
      isEmpty={(view) => isAllZero(view.rows, view.series.map((item) => item.key))}
      chart={(view) => <DailyStackedBars rows={view.rows} series={view.series} />}
      table={(view) => (
        <DataTable
          label="Audit actions per day"
          headers={["Date", ...view.series.map((item) => item.label)]}
          rows={view.rows.map((row) => [String(row.date), ...view.series.map((item) => row[item.key])])}
        />
      )}
    />
  );
}

const OUTBOX_LABELS: Record<string, string> = { pending: "Pending", leased: "Leased", done: "Done", failed: "Failed" };
const OUTBOX_ORDER = ["pending", "leased", "done", "failed"] as const;

// Outbox job states right now. This is a snapshot: the range does not change it.
export function OutboxChart() {
  const state = useLoad(async () => {
    const summary = await getOutboxSummary();
    return {
      rows: OUTBOX_ORDER.map((status) => ({
        status,
        label: OUTBOX_LABELS[status],
        jobs: summary.counts[status] ?? 0,
      })),
      oldestPendingAgeSeconds: summary.oldest_pending_age_seconds,
    };
  }, []);
  return (
    <ChartPanel
      title="Outbox health"
      description="Background jobs by state right now. Not affected by the range."
      state={state}
      isEmpty={(view) => view.rows.every((row) => row.jobs === 0)}
      chart={(view) => (
        <ChartContainer config={{ jobs: { label: "Jobs", color: "var(--chart-1)" } }} className="h-64 w-full">
          <BarChart data={view.rows} accessibilityLayer>
            <CartesianGrid vertical={false} />
            <XAxis dataKey="label" tickLine={false} axisLine={false} />
            <YAxis allowDecimals={false} tickLine={false} axisLine={false} width={40} />
            <ChartTooltip content={<ChartTooltipContent />} />
            <Bar dataKey="jobs">
              {view.rows.map((row) => (
                <Cell key={row.status} fill={row.status === "failed" ? "var(--destructive)" : "var(--chart-1)"} />
              ))}
            </Bar>
          </BarChart>
        </ChartContainer>
      )}
      table={(view) => (
        <DataTable
          label="Outbox jobs by state"
          headers={["State", "Jobs"]}
          rows={view.rows.map((row) => [row.label, row.jobs])}
        />
      )}
      footer={(view) => (
        <p className="text-sm text-muted-foreground">
          {view.oldestPendingAgeSeconds === null
            ? "No pending jobs."
            : `Oldest pending job waiting for ${formatAge(view.oldestPendingAgeSeconds)}.`}
        </p>
      )}
    />
  );
}

const USER_SERIES: Series[] = [
  { key: "active", label: "Active", color: "var(--chart-1)" },
  { key: "inactive", label: "Inactive", color: OTHER_COLOR },
];

// Accounts per role, split by status (admin only). Counts only, no names.
export function UsersChart() {
  const state = useLoad(async () => {
    const summary = await getUsersSummary();
    return {
      total: summary.total,
      pending: summary.pending_password_change,
      rows: summary.roles.map((item) => ({
        role: item.role.replace(/_/g, " "),
        active: item.active,
        inactive: item.inactive,
        total: item.active + item.inactive,
      })),
    };
  }, []);
  return (
    <ChartPanel
      title="Accounts by role"
      description="Active and inactive accounts per role. Counts only."
      state={state}
      isEmpty={(view) => view.total === 0}
      chart={(view) => (
        <ChartContainer config={configFor(USER_SERIES)} className="h-72 w-full">
          <BarChart data={view.rows} layout="vertical" accessibilityLayer>
            <CartesianGrid horizontal={false} />
            <XAxis type="number" allowDecimals={false} tickLine={false} axisLine={false} />
            <YAxis type="category" dataKey="role" width={150} tickLine={false} axisLine={false} />
            <ChartTooltip content={<ChartTooltipContent />} />
            <ChartLegend content={<ChartLegendContent />} />
            {USER_SERIES.map((item) => (
              <Bar key={item.key} dataKey={item.key} stackId="accounts" fill={`var(--color-${item.key})`} />
            ))}
          </BarChart>
        </ChartContainer>
      )}
      table={(view) => (
        <DataTable
          label="Accounts by role"
          headers={["Role", "Active", "Inactive", "Total"]}
          rows={view.rows.map((row) => [row.role, row.active, row.inactive, row.total])}
        />
      )}
      footer={(view) => (
        <p className="text-sm text-muted-foreground">
          {view.total} accounts in total. {view.pending} with a password change pending.
        </p>
      )}
    />
  );
}
