import { useState } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import {
  DEFAULT_OVERVIEW_RANGE,
  getOverviewSummary,
  Me,
  OVERVIEW_RANGES,
  OverviewRange,
  OverviewSummary,
} from "../api";
import { overviewSections } from "../overview-sections";
import { AuditChart, CasesOverTimeChart, DeadlinesChart, LoginsChart, OutboxChart, UsersChart } from "./overview/charts";
import { formatAge, pendingValue, StatCard } from "./overview/parts";
import { LoadState, useLoad } from "./overview/useLoad";

interface Props {
  profile: Me;
  notice: string | null;
}

export function Overview({ profile, notice }: Props) {
  const sections = overviewSections(profile);
  const [days, setDays] = useState<OverviewRange>(DEFAULT_OVERVIEW_RANGE);
  // One summary request per range. It returns the stat cards for the caller's sections only.
  const summary = useLoad(() => getOverviewSummary(days), [days]);
  const hasSections = sections.ownCases || sections.operations || sections.users || sections.logins;

  return (
    <>
      {notice && (
        <Alert>
          <AlertDescription>{notice}</AlertDescription>
        </Alert>
      )}
      <Card>
        <CardHeader>
          <CardTitle>Welcome, {profile.username}</CardTitle>
          <CardDescription>
            You are signed in with the <Badge variant="secondary">{profile.role}</Badge> role.
          </CardDescription>
        </CardHeader>
      </Card>

      <div className="flex flex-wrap items-center justify-between gap-3">
        <h2 className="text-lg font-semibold">Summary</h2>
        <RangeSelector value={days} onChange={setDays} />
      </div>

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
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
      </div>

      {summary.status === "error" && (
        <Alert variant="destructive">
          <AlertDescription>{summary.message}</AlertDescription>
        </Alert>
      )}

      {hasSections ? (
        <div className="space-y-6">
          {sections.operations && <CasesOverTimeChart days={days} />}
          {sections.ownCases && <DeadlinesChart />}
          {sections.operations && <AuditChart days={days} />}
          {sections.operations && <OutboxChart />}
          {sections.logins && <LoginsChart days={days} />}
          {sections.users && <UsersChart />}
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">Your role has no overview sections.</p>
      )}
    </>
  );
}

interface RangeSelectorProps {
  value: OverviewRange;
  onChange: (range: OverviewRange) => void;
}

function RangeSelector({ value, onChange }: RangeSelectorProps) {
  return (
    <div role="group" aria-label="Date range" className="flex gap-1">
      {OVERVIEW_RANGES.map((range) => (
        <Button
          key={range}
          type="button"
          size="sm"
          variant={range === value ? "default" : "outline"}
          aria-pressed={range === value}
          onClick={() => onChange(range)}
        >
          {range} days
        </Button>
      ))}
    </div>
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
