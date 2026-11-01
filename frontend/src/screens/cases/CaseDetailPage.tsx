import { ReactNode, useEffect, useState } from "react";
import { CaseDetail, getCase } from "../../api";
import { describeError } from "../../messages";
import { CaseAssignments } from "./CaseAssignments";
import { CaseClosure } from "./CaseClosure";
import { CaseDeadlines } from "./CaseDeadlines";
import { CaseEvidence } from "./CaseEvidence";
import { CaseHistory } from "./CaseHistory";
import { Timestamp } from "@/components/timestamp";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

interface Props {
  caseId: string;
}

export function CaseDetailPage({ caseId }: Props) {
  const [detail, setDetail] = useState<CaseDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Every action on the case changes the history, so the history view reloads after each one.
  const [historyRevision, setHistoryRevision] = useState(0);

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

  function applyChange(updated: CaseDetail) {
    setDetail(updated);
    setHistoryRevision((value) => value + 1);
  }

  const closed = detail?.status === "closed";

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

          <CaseClosure
            caseId={detail.id}
            status={detail.status}
            closedAt={detail.closed_at}
            closeReason={detail.close_reason}
            onChanged={applyChange}
          />

          <CaseDeadlines caseId={detail.id} deadlines={detail.deadlines} closed={closed} onChanged={applyChange} />

          <CaseAssignments caseId={detail.id} closed={closed} />

          <CaseEvidence caseId={detail.id} />

          <CaseHistory caseId={detail.id} refreshKey={historyRevision} />
        </>
      )}
    </>
  );
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
