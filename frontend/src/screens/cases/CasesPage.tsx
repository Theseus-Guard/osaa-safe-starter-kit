import { useEffect, useState } from "react";
import { toast } from "sonner";
import { CaseDetail, CasePage, listCases } from "../../api";
import { describeError } from "../../messages";
import { caseAbilities } from "../../permissions";
import { formatRiskScore } from "./case-format";
import { NewCaseDialog } from "./NewCaseDialog";
import { Timestamp } from "@/components/timestamp";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

const PAGE_SIZE = 20;
// A bounded height with a scroll container keeps the header sticky.
const TABLE_FRAME_CLASS = "overflow-hidden rounded-md border";

interface Props {
  // The caller's permission names from /auth/me.
  permissions: readonly string[];
}

export function CasesPage({ permissions }: Props) {
  const canSubmit = caseAbilities(permissions).canSubmit;
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<CasePage | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);

  useEffect(() => {
    let stale = false;
    setLoading(true);
    listCases(PAGE_SIZE, offset)
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
  }, [offset]);

  function handleCreated(created: CaseDetail) {
    toast.success(`Case ${created.reference} created.`);
    window.location.hash = `#/cases/${encodeURIComponent(created.id)}`;
  }

  const total = page?.total ?? 0;
  const shownFrom = page && page.items.length > 0 ? page.offset + 1 : 0;
  const shownTo = page ? page.offset + page.items.length : 0;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Your cases</CardTitle>
        {canSubmit && (
          <CardAction>
            <Button size="sm" onClick={() => setDialogOpen(true)}>
              New case
            </Button>
          </CardAction>
        )}
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        <div className={TABLE_FRAME_CLASS}>
          <Table>
            <TableHeader className="sticky top-0 z-10 bg-muted">
              <TableRow>
                <TableHead>Reference</TableHead>
                <TableHead>Title</TableHead>
                <TableHead>Awareness time</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Risk</TableHead>
                <TableHead>Created</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {page?.items.map((item) => (
                <TableRow key={item.id}>
                  <TableCell>
                    <a
                      href={`#/cases/${encodeURIComponent(item.id)}`}
                      className="font-mono text-sm font-medium underline-offset-4 outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring"
                    >
                      {item.reference}
                    </a>
                  </TableCell>
                  <TableCell className="whitespace-normal">{item.title}</TableCell>
                  <TableCell>
                    <Timestamp value={item.awareness_time} />
                  </TableCell>
                  <TableCell>
                    <Badge variant="secondary">{item.status}</Badge>
                  </TableCell>
                  <TableCell className={item.risk_score === null ? "text-muted-foreground" : "tabular-nums"}>
                    {formatRiskScore(item.risk_score)}
                  </TableCell>
                  <TableCell>
                    <Timestamp value={item.created_at} />
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
        {page && page.items.length === 0 && <p className="text-sm text-muted-foreground">You have not opened any cases yet.</p>}
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-sm text-muted-foreground" aria-live="polite">
            {loading ? "Loading…" : `Showing ${shownFrom}–${shownTo} of ${total}`}
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
      {canSubmit && <NewCaseDialog open={dialogOpen} onOpenChange={setDialogOpen} onCreated={handleCreated} />}
    </Card>
  );
}
