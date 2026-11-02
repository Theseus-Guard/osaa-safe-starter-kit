import { ReactNode, useEffect, useState } from "react";
import { CaseDetail, ImpactScores, getCase } from "../../api";
import { describeError } from "../../messages";
import { caseAbilities } from "../../permissions";
import { CaseAssignments } from "./CaseAssignments";
import { CaseLinks } from "./CaseLinks";
import { CaseAwarenessEvents } from "./CaseAwarenessEvents";
import { CaseClassification } from "./CaseClassification";
import { CaseClosure } from "./CaseClosure";
import { CaseDeadlines } from "./CaseDeadlines";
import { CaseDisclosures } from "./CaseDisclosures";
import { CaseEvidence } from "./CaseEvidence";
import { CaseHistory } from "./CaseHistory";
import { CaseImpacts } from "./CaseImpacts";
import { CaseLegalDuties } from "./CaseLegalDuties";
import { humanize } from "./case-format";
import { CasePreliminaryReport } from "./CasePreliminaryReport";
import { CaseReporterContact } from "./CaseReporterContact";
import { CaseReview } from "./CaseReview";
import { CaseRiskScore } from "./CaseRiskScore";
import { Tabs, type TabDefinition } from "@/components/tabs";
import { Timestamp } from "@/components/timestamp";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

interface Props {
  caseId: string;
  // The caller's permission names from /auth/me.
  permissions: readonly string[];
}

type CaseTab = "overview" | "deadlines" | "evidence" | "reviews" | "links" | "disclosures" | "activity";

export function CaseDetailPage({ caseId, permissions }: Props) {
  const abilities = caseAbilities(permissions);
  const [detail, setDetail] = useState<CaseDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<CaseTab>("overview");
  // Every action on the case changes the history, so the history view reloads after each one.
  const [historyRevision, setHistoryRevision] = useState(0);

  useEffect(() => {
    let stale = false;
    setDetail(null);
    setError(null);
    setTab("overview");
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

  // A change to the case record is audited, so the history reloads, and the preliminary report reloads with it.
  function recordChanged() {
    setHistoryRevision((value) => value + 1);
  }

  // A saved impact value updates the header at once. The change is audited, so the history reloads too.
  function applyRisk(saved: ImpactScores) {
    setDetail(
      (current) =>
        current && {
          ...current,
          risk_score: saved.risk_score,
          impact_c: saved.c,
          impact_b: saved.b,
          impact_a: saved.a,
        },
    );
    setHistoryRevision((value) => value + 1);
  }

  const closed = detail?.status === "closed";

  // Each tab holds one area. The sections a caller may not see are left out of their tab, as before.
  const tabs: TabDefinition<CaseTab>[] = [];
  if (detail) {
    tabs.push({
      id: "overview",
      label: "Overview",
      content: (
        <div className="grid items-start gap-4 xl:grid-cols-2">
          <Card>
            <CardHeader>
              <CardTitle>Key facts</CardTitle>
              <CardDescription>Times are shown in the system timezone.</CardDescription>
            </CardHeader>
            <CardContent>
              <dl className="grid gap-4 text-sm sm:grid-cols-2">
                <Detail label="Awareness time">
                  <Timestamp value={detail.awareness_time} />
                </Detail>
                <Detail label="Created">
                  <Timestamp value={detail.created_at} />
                </Detail>
                <Detail label="Calendar version">{detail.calendar_version}</Detail>
                <Detail label="Closed at">
                  {detail.closed_at ? <Timestamp value={detail.closed_at} /> : "Not closed"}
                </Detail>
              </dl>
            </CardContent>
          </Card>
          <CaseClosure
            caseId={detail.id}
            status={detail.status}
            closedAt={detail.closed_at}
            closeReason={detail.close_reason}
            reopenReason={detail.reopen_reason ?? null}
            canClose={abilities.canClose}
            onChanged={applyChange}
          />
          <CaseAssignments caseId={detail.id} closed={closed} canAssign={abilities.canAssign} />
          {abilities.canReadRecords && (
            <CaseImpacts
              caseId={detail.id}
              closed={closed}
              canWrite={abilities.canWriteRecords}
              canReadFullView={abilities.canReadFullView}
              onRecorded={recordChanged}
            />
          )}
          {abilities.canReadRecords && (
            <CaseClassification
              caseId={detail.id}
              closed={closed}
              canWrite={abilities.canWriteRecords}
              canReadFullView={abilities.canReadFullView}
              onRecorded={recordChanged}
            />
          )}
        </div>
      ),
    });
    tabs.push({
      id: "deadlines",
      label: "Deadlines",
      content: (
        <>
          <CaseDeadlines
            caseId={detail.id}
            deadlines={detail.deadlines}
            closed={closed}
            canUpdate={abilities.canUpdateDeadlines}
            canMarkNotApplicable={abilities.canMarkNotApplicable}
            onChanged={applyChange}
          />
          {abilities.canReadRecords && (
            <CaseAwarenessEvents
              caseId={detail.id}
              closed={closed}
              canWrite={abilities.canWriteRecords}
              canReadFullView={abilities.canReadFullView}
              onRecorded={recordChanged}
            />
          )}
          {abilities.canReadRecords && (
            <CaseLegalDuties
              caseId={detail.id}
              closed={closed}
              canWrite={abilities.canWriteRecords}
              canReadFullView={abilities.canReadFullView}
              onRecorded={recordChanged}
            />
          )}
        </>
      ),
    });
    if (abilities.canReadEvidence || abilities.canWriteEvidence) {
      tabs.push({
        id: "evidence",
        label: "Evidence",
        content: (
          <CaseEvidence
            caseId={detail.id}
            canRead={abilities.canReadEvidence}
            canWrite={abilities.canWriteEvidence}
          />
        ),
      });
    }
    tabs.push({
      id: "reviews",
      label: "Reviews",
      // Keyed by case, so the reviews listed never carry over from another case. Every reader sees the list.
      content: (
        <CaseReview
          key={detail.id}
          caseId={detail.id}
          closed={closed}
          canReview={abilities.canReview}
          onRecorded={() => setHistoryRevision((value) => value + 1)}
        />
      ),
    });
    tabs.push({
      id: "links",
      label: "Links and reporting",
      content: (
        <>
          <CaseLinks caseId={detail.id} closed={closed} canWrite={abilities.canWriteRecords} />
          {/* Keyed by case so a contact read for one case never shows on another. Nothing is read until the button is pressed. */}
          {abilities.canReadReporterContact && <CaseReporterContact key={detail.id} caseId={detail.id} />}
          {/* The preliminary report gathers the free text of every record, so it needs the full view. */}
          {abilities.canReadFullView && <CasePreliminaryReport caseId={detail.id} refreshKey={historyRevision} />}
        </>
      ),
    });
    // The Disclosure tab lists the package versions, their previews and their notices. Approval is offered only with
    // disclosures:approve; a notice is recorded with disclosures:approve, or with disclosures:prepare on the manual path.
    if (abilities.canReadDisclosures) {
      tabs.push({
        id: "disclosures",
        label: "Disclosure",
        content: (
          <CaseDisclosures
            key={detail.id}
            caseId={detail.id}
            closed={closed}
            canApprove={abilities.canApproveDisclosures}
            canRecordNotices={abilities.canRecordNotices}
          />
        ),
      });
    }
    tabs.push({
      id: "activity",
      label: "Activity",
      content: <CaseHistory caseId={detail.id} refreshKey={historyRevision} />,
    });
  }

  return (
    <>
      <div className="flex flex-wrap items-center justify-between gap-3">
        <Button variant="outline" size="sm" render={<a href="#/cases" />}>
          Back to cases
        </Button>
      </div>

      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      {detail === null && error === null && <p className="text-sm text-muted-foreground">Loading case…</p>}

      {detail && (
        <>
          <Card>
            <CardContent className="flex flex-col gap-6 md:flex-row md:items-start md:justify-between">
              <div className="flex min-w-0 flex-col gap-2">
                <p className="font-mono text-sm text-muted-foreground">{detail.reference}</p>
                <div className="flex flex-wrap items-center gap-3">
                  <h2 className="text-xl font-semibold tracking-tight break-words">{detail.title}</h2>
                  <Badge variant="secondary">{humanize(detail.status)}</Badge>
                </div>
              </div>
              <CaseRiskScore
                caseId={detail.id}
                risk={detail}
                closed={closed}
                canWrite={abilities.canWriteRecords}
                onSaved={applyRisk}
              />
            </CardContent>
          </Card>

          <Tabs label="Case sections" tabs={tabs} value={tab} onChange={setTab} />
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
