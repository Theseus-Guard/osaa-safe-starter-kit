import { useEffect, useState } from "react";
import { Search } from "lucide-react";
import { toast } from "sonner";
import { CASE_STATUSES, CaseDetail, CasePage, CaseStatus, listCases } from "../../api";
import { describeError } from "../../messages";
import { caseAbilities, canReadCases } from "../../permissions";
import { formatRiskScore, humanize, isOverdue } from "./case-format";
import { NewCaseDialog } from "./NewCaseDialog";
import { Timestamp } from "@/components/timestamp";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardFooter } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

const PAGE_SIZE = 20;
// Typing waits this long before the search is sent, so each keystroke does not reach the server.
const SEARCH_DELAY_MS = 250;
const COLUMN_COUNT = 8;
// The status filter's value when every status is shown. It is not a case status, so it is not sent to the server.
const ALL_STATUSES = "all";
type StatusFilter = CaseStatus | typeof ALL_STATUSES;

interface Props {
  // The caller's permission names from /auth/me.
  permissions: readonly string[];
}

export function CasesPage({ permissions }: Props) {
  if (!canReadCases(permissions)) return <NoCaseAccess isAdmin={permissions.includes("users:manage")} />;
  return <CaseList permissions={permissions} />;
}

// Shown instead of the list for accounts without a case permission, so the page does not fail with a bare 403.
function NoCaseAccess({ isAdmin }: { isAdmin: boolean }) {
  return (
    <Alert>
      <AlertDescription>
        {isAdmin
          ? "Case records are not shown to administrators. Use Users and Roles to manage accounts, and Overview for counts."
          : "Your role does not include access to case records."}
      </AlertDescription>
    </Alert>
  );
}

function CaseList({ permissions }: Props) {
  const canSubmit = caseAbilities(permissions).canSubmit;
  const [query, setQuery] = useState("");
  // The trimmed search that is sent to the server. Changing it starts again at the first page.
  const [term, setTerm] = useState("");
  const [statusFilter, setStatusFilter] = useState<StatusFilter>(ALL_STATUSES);
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<CasePage | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);

  useEffect(() => {
    const timer = window.setTimeout(() => {
      setTerm(query.trim());
      setOffset(0);
    }, SEARCH_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    let stale = false;
    setLoading(true);
    listCases(PAGE_SIZE, offset, term, statusFilter === ALL_STATUSES ? undefined : statusFilter)
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
  }, [offset, term, statusFilter]);

  function handleCreated(created: CaseDetail) {
    toast.success(`Case ${created.reference} created.`);
    window.location.hash = `#/cases/${encodeURIComponent(created.id)}`;
  }

  function chooseStatus(value: StatusFilter) {
    setStatusFilter(value);
    setOffset(0);
  }

  const total = page?.total ?? 0;
  const shownFrom = page && page.items.length > 0 ? page.offset + 1 : 0;
  const shownTo = page ? page.offset + page.items.length : 0;
  const searching = term !== "";
  const filtering = statusFilter !== ALL_STATUSES;
  // Read once per render, so the Overdue marks are current whenever the list is shown.
  const now = new Date();

  return (
    <>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground" aria-live="polite">
          {page !== null && `${total} ${total === 1 ? "case" : "cases"}${searching ? " found" : ""}`}
        </p>
        {canSubmit && <Button onClick={() => setDialogOpen(true)}>New case</Button>}
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <div className="relative w-full sm:max-w-sm">
          <Search aria-hidden="true" className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            type="search"
            aria-label="Search cases"
            placeholder="Search by reference"
            className="pl-8"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </div>
        <Select
          value={statusFilter}
          onValueChange={(value) => {
            if (value) chooseStatus(value as StatusFilter);
          }}
        >
          <SelectTrigger aria-label="Filter by status" className="w-40">
            <SelectValue>
              {(value: string | null) => (value === null || value === ALL_STATUSES ? "All statuses" : humanize(value))}
            </SelectValue>
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ALL_STATUSES}>All statuses</SelectItem>
            {CASE_STATUSES.map((status) => (
              <SelectItem key={status} value={status}>
                {humanize(status)}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      <Card className="gap-0 py-0">
        <Table>
          <TableHeader className="bg-muted/50">
            <TableRow>
              <TableHead className="px-4">Reference</TableHead>
              <TableHead>Title</TableHead>
              <TableHead>Status</TableHead>
              <TableHead>Risk score</TableHead>
              <TableHead>Awareness time</TableHead>
              <TableHead>Next deadline</TableHead>
              <TableHead>Last updated</TableHead>
              <TableHead className="pr-4">Created</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {page === null && (
              <TableRow>
                <TableCell colSpan={COLUMN_COUNT} className="h-24 px-4 text-center text-muted-foreground">
                  Loading cases…
                </TableCell>
              </TableRow>
            )}
            {page?.items.map((item) => (
              <TableRow key={item.id}>
                <TableCell className="px-4">
                  <a
                    href={`#/cases/${encodeURIComponent(item.id)}`}
                    className="font-mono text-sm font-medium text-primary underline-offset-4 outline-none hover:underline focus-visible:ring-3 focus-visible:ring-ring"
                  >
                    {item.reference}
                  </a>
                </TableCell>
                <TableCell className="max-w-96 font-medium whitespace-normal">{item.title}</TableCell>
                <TableCell>
                  <Badge variant="secondary">{humanize(item.status)}</Badge>
                </TableCell>
                <TableCell className={item.risk_score === null ? "text-muted-foreground" : "tabular-nums"}>
                  {formatRiskScore(item.risk_score)}
                </TableCell>
                <TableCell className="text-muted-foreground">
                  <Timestamp value={item.awareness_time} />
                </TableCell>
                <TableCell className={item.next_deadline === null ? "text-muted-foreground" : undefined}>
                  {item.next_deadline === null ? (
                    "None"
                  ) : (
                    <span className="flex flex-wrap items-center gap-2">
                      <Timestamp value={item.next_deadline} />
                      {isOverdue(item.next_deadline, now) && <Badge variant="destructive">Overdue</Badge>}
                    </span>
                  )}
                </TableCell>
                <TableCell className="text-muted-foreground">
                  <Timestamp value={item.updated_at} />
                </TableCell>
                <TableCell className="pr-4 text-muted-foreground">
                  <Timestamp value={item.created_at} />
                </TableCell>
              </TableRow>
            ))}
            {page !== null && page.items.length === 0 && (
              <TableRow>
                <TableCell colSpan={COLUMN_COUNT} className="h-24 px-4 text-center text-muted-foreground">
                  {searching
                    ? "No cases match this search."
                    : filtering
                      ? "No cases have this status."
                      : "You have not opened any cases yet."}
                </TableCell>
              </TableRow>
            )}
          </TableBody>
        </Table>
        <CardFooter className="justify-between gap-2 py-3">
          <p className="text-sm text-muted-foreground" aria-live="polite">
            {loading ? "Loading…" : total > 0 ? `Showing ${shownFrom}–${shownTo} of ${total}` : ""}
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
        </CardFooter>
      </Card>

      {canSubmit && <NewCaseDialog open={dialogOpen} onOpenChange={setDialogOpen} onCreated={handleCreated} />}
    </>
  );
}
