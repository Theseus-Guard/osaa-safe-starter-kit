import { useEffect, useState } from "react";
import { HistoryPage, getCaseHistory } from "../../api";
import { describeError } from "../../messages";
import { describeDetails, historyActionLabel } from "./case-format";
import { Timestamp } from "@/components/timestamp";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

const PAGE_SIZE = 20;

interface Props {
  caseId: string;
  // Changes when the case changes, so the history reloads after an action on this page.
  refreshKey: number;
}

export function CaseHistory({ caseId, refreshKey }: Props) {
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<HistoryPage | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let stale = false;
    setLoading(true);
    getCaseHistory(caseId, PAGE_SIZE, offset)
      .then((result) => {
        if (stale) return;
        setPage(result);
        setError(null);
      })
      .catch((caught: unknown) => {
        if (!stale) setError(describeError(caught));
      })
      .finally(() => {
        if (!stale) setLoading(false);
      });
    return () => {
      stale = true;
    };
  }, [caseId, offset, refreshKey]);

  const shownFrom = page && page.items.length > 0 ? page.offset + 1 : 0;
  const shownTo = page ? page.offset + page.items.length : 0;

  return (
    <Card>
      <CardHeader>
        <CardTitle>History</CardTitle>
        <CardDescription>
          Actor and details are shown only when your account has full access to the case.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {page && page.items.length === 0 && <p className="text-sm text-muted-foreground">No history yet.</p>}
        {page && page.items.length > 0 && (
          <div className="max-h-[50vh] overflow-auto rounded-md border [&_[data-slot=table-container]]:overflow-visible">
            <Table>
              <TableHeader className="sticky top-0 z-10 bg-muted">
                <TableRow>
                  <TableHead>When</TableHead>
                  <TableHead>Action</TableHead>
                  <TableHead>Actor</TableHead>
                  <TableHead>Details</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {page.items.map((entry) => {
                  const lines = describeDetails(entry.details);
                  return (
                    <TableRow key={entry.id}>
                      <TableCell>
                        <Timestamp value={entry.created_at} />
                      </TableCell>
                      <TableCell>{historyActionLabel(entry.action)}</TableCell>
                      <TableCell className="font-mono text-xs">{entry.actor_id ?? "Not shown"}</TableCell>
                      <TableCell className="whitespace-normal">
                        {entry.details === null && <span className="text-muted-foreground">Not shown</span>}
                        {entry.details !== null && lines.length === 0 && (
                          <span className="text-muted-foreground">None recorded</span>
                        )}
                        {lines.length > 0 && (
                          <ul className="flex flex-col gap-1 text-xs">
                            {lines.map((line) => (
                              <li key={line}>{line}</li>
                            ))}
                          </ul>
                        )}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>
        )}
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-sm text-muted-foreground" aria-live="polite">
            {loading ? "Loading…" : `Showing ${shownFrom}–${shownTo} of ${page?.total ?? 0}`}
          </p>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={loading || !page || page.offset === 0}
              onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            >
              Previous
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={loading || !page || page.offset + page.limit >= page.total}
              onClick={() => setOffset(offset + PAGE_SIZE)}
            >
              Next
            </Button>
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
