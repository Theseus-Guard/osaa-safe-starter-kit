import { useEffect, useState } from "react";
import { PreliminaryReport, getPreliminaryReport } from "../../api";
import { describeError } from "../../messages";
import { humanize, missingFieldLabel, recordText, triggerLabel } from "./case-format";
import { Timestamp } from "@/components/timestamp";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

interface Props {
  caseId: string;
  // Bumped by the case page after a change to the case record, so the report reloads from the server.
  refreshKey: number;
}

// Shown only to callers with cases:read_all. The page does not render this component for other callers.
// The report carries the contact status only ("recorded" or "missing"). The contact itself is never shown here.
export function CasePreliminaryReport({ caseId, refreshKey }: Props) {
  const [report, setReport] = useState<PreliminaryReport | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let stale = false;
    setError(null);
    getPreliminaryReport(caseId)
      .then((result) => {
        if (!stale) setReport(result);
      })
      .catch((caught: unknown) => {
        if (!stale) setError(describeError(caught));
      });
    return () => {
      stale = true;
    };
  }, [caseId, refreshKey]);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Preliminary report</CardTitle>
        <CardDescription>
          Generated from the case record. Missing fields are listed; the report is produced anyway.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-6 text-sm">
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {report === null && error === null && <p className="text-muted-foreground">Loading report…</p>}
        {report !== null && <ReportBody report={report} />}
      </CardContent>
    </Card>
  );
}

function ReportBody({ report }: { report: PreliminaryReport }) {
  return (
    <>
      <div className="flex flex-wrap items-center gap-3">
        <Badge variant={report.complete ? "secondary" : "outline"}>{report.complete ? "Complete" : "Incomplete"}</Badge>
        <span className="text-muted-foreground">
          Generated <Timestamp value={report.generated_at} />
        </span>
      </div>

      <section className="grid gap-2">
        <h3 className="font-medium">Missing fields</h3>
        {report.missing_fields.length === 0 ? (
          <p className="text-muted-foreground">Nothing is missing.</p>
        ) : (
          <ul className="list-inside list-disc text-muted-foreground">
            {report.missing_fields.map((entry) => (
              <li key={entry.field}>{missingFieldLabel(entry)}</li>
            ))}
          </ul>
        )}
      </section>

      <section className="grid gap-2">
        <h3 className="font-medium">Reporter contact</h3>
        <p className="text-muted-foreground">{report.contact.status === "recorded" ? "Recorded" : "Missing"}</p>
      </section>

      <section className="grid gap-2">
        <h3 className="font-medium">Classification</h3>
        {report.classification.version === 0 ? (
          <p className="text-muted-foreground">No classification is recorded.</p>
        ) : (
          <dl className="grid gap-2 sm:grid-cols-2">
            <div>
              <dt className="font-medium">Version</dt>
              <dd className="text-muted-foreground">{report.classification.version}</dd>
            </div>
            <div>
              <dt className="font-medium">Triggers</dt>
              <dd className="text-muted-foreground">
                {report.classification.triggers.length === 0
                  ? "None selected"
                  : report.classification.triggers.map(triggerLabel).join(", ")}
              </dd>
            </div>
            <div>
              <dt className="font-medium">Event</dt>
              <dd className="text-muted-foreground">
                {report.classification.event_kind === null ? "Not recorded" : humanize(report.classification.event_kind)}
              </dd>
            </div>
            <div>
              <dt className="font-medium">Internal incident option</dt>
              <dd className="text-muted-foreground">{report.classification.internal_incident_option ? "Yes" : "No"}</dd>
            </div>
            <div>
              <dt className="font-medium">Rationale</dt>
              <dd className="whitespace-pre-wrap text-muted-foreground">
                {recordText(report.classification.classification_rationale, true)}
              </dd>
            </div>
            <div>
              <dt className="font-medium">Responsible operator</dt>
              <dd className="whitespace-pre-wrap text-muted-foreground">
                {recordText(report.classification.responsible_operator, true)}
              </dd>
            </div>
            <div>
              <dt className="font-medium">Affected versions</dt>
              <dd className="whitespace-pre-wrap text-muted-foreground">
                {recordText(report.classification.affected_versions, true)}
              </dd>
            </div>
            <div>
              <dt className="font-medium">Approved scope</dt>
              <dd className="whitespace-pre-wrap text-muted-foreground">
                {recordText(report.classification.approved_scope, true)}
              </dd>
            </div>
            <div>
              <dt className="font-medium">Source of the scope permission</dt>
              <dd className="whitespace-pre-wrap text-muted-foreground">
                {recordText(report.classification.scope_permission_source, true)}
              </dd>
            </div>
            <div>
              <dt className="font-medium">Containment</dt>
              <dd className="whitespace-pre-wrap text-muted-foreground">
                {recordText(report.classification.containment, true)}
              </dd>
            </div>
            <div>
              <dt className="font-medium">Evidence gap plan</dt>
              <dd className="whitespace-pre-wrap text-muted-foreground">
                {recordText(report.classification.evidence_gap_plan, true)}
              </dd>
            </div>
          </dl>
        )}
      </section>

      <section className="grid gap-2">
        <h3 className="font-medium">Awareness</h3>
        {report.awareness_events.length === 0 ? (
          <p className="text-muted-foreground">No awareness event is recorded.</p>
        ) : (
          <ul className="grid gap-2">
            {report.awareness_events.map((event) => (
              <li key={event.id} className="grid gap-1">
                <span>
                  {humanize(event.awareness_kind)} · {humanize(event.event_kind)} · <Timestamp value={event.occurred_at} />
                </span>
                <span className="whitespace-pre-wrap text-muted-foreground">{event.basis}</span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="grid gap-2">
        <h3 className="font-medium">Impact areas</h3>
        <ul className="grid gap-2">
          {report.impacts.map((impact) => (
            <li key={impact.area} className="grid gap-1">
              <span>
                {humanize(impact.area)}:{" "}
                {impact.assessed && impact.severity !== null ? humanize(impact.severity) : "Not assessed"}
              </span>
              {impact.assessed && <span className="whitespace-pre-wrap text-muted-foreground">{impact.scope}</span>}
            </li>
          ))}
        </ul>
      </section>

      <section className="grid gap-2">
        <h3 className="font-medium">Legal duties</h3>
        {report.legal_duties.length === 0 ? (
          <p className="text-muted-foreground">No legal duty is recorded.</p>
        ) : (
          <ul className="grid gap-2">
            {report.legal_duties.map((duty) => (
              <li key={duty.id} className="grid gap-1">
                <span>
                  {humanize(duty.kind)} · {humanize(duty.status)} · due <Timestamp value={duty.due_at} />
                </span>
                <span className="whitespace-pre-wrap text-muted-foreground">
                  {duty.description}
                  {" — "}
                  {duty.source_reference}
                  {duty.jurisdiction !== null && ` (${duty.jurisdiction})`}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section className="grid gap-2">
        <h3 className="font-medium">SAFE deadline tasks</h3>
        {report.safe_deadlines.length === 0 ? (
          <p className="text-muted-foreground">No deadline tasks.</p>
        ) : (
          <ul className="grid gap-1">
            {report.safe_deadlines.map((task) => (
              <li key={`${task.kind}-${task.due_at}`}>
                {humanize(task.kind)} · <Timestamp value={task.due_at} /> · {humanize(task.status)}
              </li>
            ))}
          </ul>
        )}
      </section>
    </>
  );
}
