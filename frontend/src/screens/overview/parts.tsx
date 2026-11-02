import { useState, type ReactNode } from "react";
import { TriangleAlert } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  Card,
  CardAction,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { SegmentedControl } from "@/components/segmented-control";
import type { LoadState } from "./useLoad";

export const NO_DATA_MESSAGE = "No data in this period.";

interface StatCardProps {
  label: string;
  value: string;
  hint: string;
  // Set for a state that needs attention. The text and the icon say so; colour is not the only signal.
  warning?: boolean;
}

export function StatCard({ label, value, hint, warning = false }: StatCardProps) {
  return (
    <Card>
      <CardHeader>
        <CardDescription>{label}</CardDescription>
        <CardTitle className={`text-2xl tabular-nums ${warning ? "text-destructive" : ""}`}>{value}</CardTitle>
      </CardHeader>
      <CardContent>
        <p className="flex items-center gap-1.5 text-sm text-muted-foreground">
          {warning && <TriangleAlert aria-hidden="true" className="size-4 shrink-0" />}
          <span>{hint}</span>
        </p>
      </CardContent>
    </Card>
  );
}

// The value shown by a stat card while its data loads or fails. Null once the data is ready.
export function pendingValue<T>(state: LoadState<T>): string | null {
  if (state.status === "loading") return "Loading…";
  if (state.status === "error") return "Unavailable";
  return null;
}

// How many rows a table view shows before "Show all" is pressed.
export const TABLE_PREVIEW_ROWS = 8;

// Most x-axis labels a date chart shows. Ticks are skipped beyond this, so labels never stack or overlap.
const MAX_AXIS_LABELS = 6;

// The Recharts interval for a series of this length: show every (interval + 1)th point.
export function axisInterval(pointCount: number): number {
  return Math.max(0, Math.ceil(pointCount / MAX_AXIS_LABELS) - 1);
}

const CHART_DATE = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", timeZone: "UTC" });

// Axis and tooltip label for a YYYY-MM-DD date, for example "15 Oct". Parsed as UTC so the day never shifts.
export function formatChartDate(date: string): string {
  return CHART_DATE.format(new Date(`${date}T00:00:00Z`));
}

// Seconds as a short label: "4 s", "3 min", "2 h", "5 d".
export function formatAge(seconds: number): string {
  if (seconds < 60) return `${seconds} s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h`;
  return `${Math.floor(seconds / 86400)} d`;
}

type ChartView = "chart" | "table";

const VIEW_OPTIONS: { value: ChartView; label: string }[] = [
  { value: "chart", label: "Chart" },
  { value: "table", label: "Table" },
];

interface ChartPanelProps<T> {
  title: string;
  description: string;
  state: LoadState<T>;
  isEmpty: (data: T) => boolean;
  chart: (data: T) => ReactNode;
  table: (data: T) => ReactNode;
  footer?: (data: T) => ReactNode;
  // Spans both columns on wide screens. Used when the card would otherwise stand alone in its row.
  wide?: boolean;
}

// One card per chart. The Chart / Table toggle shows one view at a time. Loading, error and empty states are rendered here.
export function ChartPanel<T>({ title, description, state, isEmpty, chart, table, footer, wide = false }: ChartPanelProps<T>) {
  const [view, setView] = useState<ChartView>("chart");
  const hasData = state.status === "ready" && !isEmpty(state.data);

  return (
    <Card className={wide ? "xl:col-span-2" : undefined}>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <CardDescription>{description}</CardDescription>
        {hasData && (
          <CardAction>
            <SegmentedControl label={`${title} view`} options={VIEW_OPTIONS} value={view} onChange={setView} />
          </CardAction>
        )}
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {state.status === "loading" && (
          <p role="status" className="text-sm text-muted-foreground">
            Loading…
          </p>
        )}
        {state.status === "error" && (
          <Alert variant="destructive">
            <AlertDescription>{state.message}</AlertDescription>
          </Alert>
        )}
        {state.status === "ready" &&
          (hasData ? (
            <div className="min-w-0">{view === "chart" ? chart(state.data) : table(state.data)}</div>
          ) : (
            <p className="text-sm text-muted-foreground">{NO_DATA_MESSAGE}</p>
          ))}
        {state.status === "ready" && footer && footer(state.data)}
      </CardContent>
    </Card>
  );
}

// The responsive grid of stat cards: as many columns as fit, each at least 12 rem wide.
export const STAT_GRID_CLASS = "grid gap-4 grid-cols-[repeat(auto-fit,minmax(12rem,1fr))]";
// The responsive grid of chart cards: two columns on wide screens, one on narrow ones.
export const CHART_GRID_CLASS = "grid gap-4 xl:grid-cols-2";
