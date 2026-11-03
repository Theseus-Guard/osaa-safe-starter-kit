import { Fragment, ReactNode, useState } from "react";
import { SegmentedControl } from "@/components/segmented-control";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import {
  DEFAULT_OVERVIEW_RANGE,
  getOverviewSummary,
  Me,
  OVERVIEW_RANGES,
  OverviewRange,
  OverviewSummary,
} from "../api";
import { overviewSections } from "../overview-sections";
import {
  ApprovalsPanel,
  AuditChart,
  CasesOverTimeChart,
  DeadlinesChart,
  formatDays,
  LoginsChart,
  MyCasesChart,
  MyContributionsChart,
  OutboxChart,
  UsersChart,
  WeeklyUpdatesPanel,
} from "./overview/charts";
import { CHART_GRID_CLASS, formatAge, formatChartDate, pendingValue, STAT_GRID_CLASS, StatCard } from "./overview/parts";
import { type LoadState, useLoad } from "./overview/useLoad";

interface Props {
  profile: Me;
  notice: string | null;
}

const RANGE_OPTIONS = OVERVIEW_RANGES.map((range) => ({ value: range, label: `${range} days` }));

interface ChartEntry {
  key: string;
  // Receives whether the card should span both columns, so the last card never stands alone in a row.
  render: (wide: boolean) => ReactNode;
}

export function Overview({ profile, notice }: Props) {
  const sections = overviewSections(profile);
  const [days, setDays] = useState<OverviewRange>(DEFAULT_OVERVIEW_RANGE);
  // One summary request per range. It returns the stat cards for the caller's sections only.
  const summary = useLoad(() => getOverviewSummary(days), [days]);
  const hasSections =
    sections.ownCases || sections.operations || sections.users || sections.logins || sections.approvals || sections.weeklyUpdates;

  const charts: ChartEntry[] = [];
  if (sections.operations) charts.push({ key: "cases", render: (wide) => <CasesOverTimeChart days={days} wide={wide} /> });
  if (sections.ownCases) charts.push({ key: "my-cases", render: (wide) => <MyCasesChart days={days} wide={wide} /> });
  if (sections.ownCases) {
    charts.push({ key: "my-contributions", render: (wide) => <MyContributionsChart days={days} wide={wide} /> });
  }
  if (sections.ownCases) charts.push({ key: "deadlines", render: (wide) => <DeadlinesChart wide={wide} /> });
  if (sections.approvals) charts.push({ key: "approvals", render: (wide) => <ApprovalsPanel state={summary} wide={wide} /> });
  if (sections.weeklyUpdates) {
    charts.push({ key: "weekly", render: (wide) => <WeeklyUpdatesPanel state={summary} wide={wide} /> });
  }
  if (sections.operations) charts.push({ key: "audit", render: (wide) => <AuditChart days={days} wide={wide} /> });
  if (sections.operations) charts.push({ key: "outbox", render: (wide) => <OutboxChart wide={wide} /> });
  if (sections.logins) charts.push({ key: "logins", render: (wide) => <LoginsChart days={days} wide={wide} /> });
  if (sections.users) charts.push({ key: "users", render: (wide) => <UsersChart wide={wide} /> });

  return (
    <>
      {notice && (
        <Alert>
          <AlertDescription>{notice}</AlertDescription>
        </Alert>
      )}

      {/* The page title and description come from the shell. The range applies to every card below. */}
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          Welcome, <span className="font-medium text-foreground">{profile.username}</span>. You are signed in with the{" "}
          <Badge variant="secondary">{profile.role}</Badge> role.
        </p>
        <SegmentedControl label="Date range" options={RANGE_OPTIONS} value={days} onChange={setDays} />
      </div>

      <section aria-labelledby="overview-summary" className="flex flex-col gap-3">
        <h2 id="overview-summary" className="text-lg font-semibold tracking-tight">
          Summary
        </h2>
        <div className={STAT_GRID_CLASS}>
          <StatCard
            label="Password change"
            value={profile.password_change_required ? "Required" : "Not required"}
            hint={
              profile.password_change_required
                ? "You must set a new password before you continue."
                : "No action needed."
            }
          />
          {sections.ownCases && <OwnCaseCards state={summary} days={days} />}
          {sections.operations && <OperationsCards state={summary} days={days} />}
          {sections.users && <UserCards state={summary} />}
          {sections.approvals && <ApprovalCards state={summary} />}
          {sections.weeklyUpdates && <WeeklyCards state={summary} />}
        </div>
        {summary.status === "error" && (
          <Alert variant="destructive">
            <AlertDescription>{summary.message}</AlertDescription>
          </Alert>
        )}
      </section>

      {hasSections ? (
        <div className={CHART_GRID_CLASS}>
          {charts.map((chart, index) => (
            <Fragment key={chart.key}>
              {chart.render(charts.length % 2 === 1 && index === charts.length - 1)}
            </Fragment>
          ))}
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">Your role has no overview sections.</p>
      )}
    </>
  );
}

interface SummaryCardProps {
  label: string;
  // Undefined when the section is missing from the answer, which the screen shows as unavailable.
  count: number | undefined;
  hint: string;
}

// A count card. While the summary loads or fails, the value says so and the hint stays empty.
function SummaryCard({ state, label, count, hint }: SummaryCardProps & { state: LoadState<OverviewSummary> }) {
  const pending = pendingValue(state);
  const value = pending ?? (count === undefined ? "Unavailable" : String(count));
  return <StatCard label={label} value={value} hint={pending === null && count !== undefined ? hint : ""} />;
}

function OwnCaseCards({ state, days }: { state: LoadState<OverviewSummary>; days: OverviewRange }) {
  const own = state.status === "ready" ? state.data.own_cases : null;
  return (
    <>
      <SummaryCard
        state={state}
        label="My open cases"
        count={own?.open_cases}
        hint={`${own?.opened_in_period ?? 0} opened in the last ${days} days`}
      />
      <SummaryCard
        state={state}
        label="My deadlines due in 72 hours"
        count={own?.deadlines_due_72h}
        hint="Open tasks on my open cases."
      />
      <SummaryCard
        state={state}
        label="My overdue deadlines"
        count={own?.deadlines_overdue}
        hint={own && own.deadlines_overdue > 0 ? "Past due and still open." : "None past due."}
      />
    </>
  );
}

function OperationsCards({ state, days }: { state: LoadState<OverviewSummary>; days: OverviewRange }) {
  const activity = state.status === "ready" ? state.data.case_activity : null;
  const ops = state.status === "ready" ? state.data.ops : null;
  const pending = pendingValue(state);
  return (
    <>
      <SummaryCard
        state={state}
        label="Open cases (all)"
        count={activity?.open_cases}
        hint={`${activity?.opened_in_period ?? 0} opened in the last ${days} days`}
      />
      <SummaryCard
        state={state}
        label="Failed background jobs"
        count={ops?.failed_outbox_jobs}
        hint={ops && ops.failed_outbox_jobs > 0 ? "Jobs that used up their retries." : "None failed."}
      />
      {pending !== null || ops === null ? (
        <StatCard label="Worker heartbeat" value={pending ?? "Unavailable"} hint="" />
      ) : ops.status === "stale" ? (
        <StatCard
          label="Worker heartbeat"
          value="Stale"
          warning
          hint={`Last heartbeat ${formatAge(ops.heartbeat_age_seconds ?? 0)} ago.`}
        />
      ) : ops.status === "no_data" ? (
        <StatCard label="Worker heartbeat" value="No heartbeat" hint="The worker has not reported yet." />
      ) : (
        <StatCard
          label="Worker heartbeat"
          value={`${formatAge(ops.heartbeat_age_seconds ?? 0)} ago`}
          hint="Received from the background worker."
        />
      )}
    </>
  );
}

function ApprovalCards({ state }: { state: LoadState<OverviewSummary> }) {
  const approvals = state.status === "ready" ? state.data.approvals : null;
  const pending = pendingValue(state);
  const oldest = approvals?.oldest_pending_age_days ?? null;
  return (
    <>
      <SummaryCard
        state={state}
        label="Versions awaiting approval"
        count={approvals?.pending}
        hint={approvals && approvals.pending > 0 ? "Latest versions with no approval yet." : "Nothing is waiting."}
      />
      <StatCard
        label="Oldest waiting version"
        value={pending ?? (approvals ? formatDays(oldest) : "Unavailable")}
        hint={pending === null && approvals ? "Age of the oldest version still waiting." : ""}
      />
    </>
  );
}

function WeeklyCards({ state }: { state: LoadState<OverviewSummary> }) {
  const weekly = state.status === "ready" ? state.data.weekly_updates : null;
  const latest = weekly?.latest_update_date ?? null;
  return (
    <SummaryCard
      state={state}
      label="Significant risks still open"
      count={weekly?.open_significant_risk_cases}
      hint={weekly ? (latest ? `Latest weekly update on ${formatChartDate(latest)}.` : "No weekly update yet.") : ""}
    />
  );
}

function UserCards({ state }: { state: LoadState<OverviewSummary> }) {
  const users = state.status === "ready" ? state.data.users : null;
  return (
    <SummaryCard
      state={state}
      label="Pending password changes"
      count={users?.pending_password_change}
      hint="Accounts that must set a new password."
    />
  );
}
