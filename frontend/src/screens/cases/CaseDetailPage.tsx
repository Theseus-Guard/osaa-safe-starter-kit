import { ReactNode, useEffect, useState } from "react";
import { CaseDetail, getCase } from "../../api";
import { describeError } from "../../messages";
import { Timestamp } from "@/components/timestamp";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

interface Props {
  caseId: string;
}

export function CaseDetailPage({ caseId }: Props) {
  const [detail, setDetail] = useState<CaseDetail | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let stale = false;
    setDetail(null);
    setError(null);
    getCase(caseId)
      .then((result) => {
        if (!stale) setDetail(result);
      })
      .catch((caught: unknown) => {
        if (!stale) setError(describeError(caught));
      });
    return () => {
      stale = true;
    };
  }, [caseId]);

  return (
    <>
      <Button variant="outline" size="sm" className="self-start" render={<a href="#/cases" />}>
        Back to cases
      </Button>

      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      {detail === null && error === null && <p className="text-sm text-muted-foreground">Loading case…</p>}

      {detail && (
        <>
          <Card>
            <CardHeader>
              <CardTitle>{detail.title}</CardTitle>
              <CardDescription className="font-mono">{detail.reference}</CardDescription>
            </CardHeader>
            <CardContent>
              <dl className="grid gap-4 text-sm sm:grid-cols-2">
                <Detail label="Status">
                  <Badge variant="secondary">{detail.status}</Badge>
                </Detail>
                <Detail label="Awareness time">
                  <Timestamp value={detail.awareness_time} />
                </Detail>
                <Detail label="Created">
                  <Timestamp value={detail.created_at} />
                </Detail>
                <Detail label="Calendar version">{detail.calendar_version}</Detail>
              </dl>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Deadline tasks</CardTitle>
              <CardDescription>Due times are shown in the system timezone.</CardDescription>
            </CardHeader>
            <CardContent>
              {detail.deadlines.length === 0 ? (
                <p className="text-sm text-muted-foreground">No deadline tasks.</p>
              ) : (
                <div className="overflow-auto rounded-md border [&_[data-slot=table-container]]:overflow-visible">
                  <Table>
                    <TableHeader className="bg-muted">
                      <TableRow>
                        <TableHead>Task</TableHead>
                        <TableHead>Due</TableHead>
                        <TableHead>Status</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {detail.deadlines.map((task) => (
                        <TableRow key={`${task.kind}-${task.due_at}`}>
                          <TableCell>{kindLabel(task.kind)}</TableCell>
                          <TableCell>
                            <Timestamp value={task.due_at} />
                          </TableCell>
                          <TableCell>
                            <Badge variant="secondary">{task.status}</Badge>
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </div>
              )}
            </CardContent>
          </Card>
        </>
      )}
    </>
  );
}

// "preliminary_safe_report" -> "Preliminary safe report"
function kindLabel(kind: string): string {
  const words = kind.replace(/_/g, " ");
  return words.charAt(0).toUpperCase() + words.slice(1);
}

interface DetailProps {
  label: string;
  children: ReactNode;
}

function Detail({ label, children }: DetailProps) {
  return (
    <div className="grid gap-1">
      <dt className="font-medium">{label}</dt>
      <dd className="text-muted-foreground">{children}</dd>
    </div>
  );
}
