import type { ReactNode } from "react";
import { TriangleAlert } from "lucide-react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
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
        <CardTitle className={`text-2xl ${warning ? "text-destructive" : ""}`}>{value}</CardTitle>
      </CardHeader>
      <CardContent>
        <p className="flex items-center gap-1 text-sm text-muted-foreground">
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

interface ChartPanelProps<T> {
  title: string;
  description: string;
  state: LoadState<T>;
  isEmpty: (data: T) => boolean;
  chart: (data: T) => ReactNode;
  table: (data: T) => ReactNode;
  footer?: (data: T) => ReactNode;
}

// One chart with its table view next to it. Loading, error and empty states are rendered here, once.
export function ChartPanel<T>({ title, description, state, isEmpty, chart, table, footer }: ChartPanelProps<T>) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>{title}</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
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
          (isEmpty(state.data) ? (
            <p className="text-sm text-muted-foreground">{NO_DATA_MESSAGE}</p>
          ) : (
            <div className="grid gap-6 lg:grid-cols-2">
              <div className="min-w-0" role="group" aria-label={`${title} chart`}>
                {chart(state.data)}
              </div>
              <div className="min-w-0 overflow-x-auto">{table(state.data)}</div>
            </div>
          ))}
        {state.status === "ready" && footer && footer(state.data)}
      </CardContent>
    </Card>
  );
}

// Seconds as a short label: "4 s", "3 min", "2 h", "5 d".
export function formatAge(seconds: number): string {
  if (seconds < 60) return `${seconds} s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} h`;
  return `${Math.floor(seconds / 86400)} d`;
}

// Chart axis labels: the day part of a YYYY-MM-DD date.
export function formatDay(date: string): string {
  return date.slice(5);
}
