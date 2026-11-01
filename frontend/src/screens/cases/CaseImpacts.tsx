import { FormEvent, useEffect, useState } from "react";
import { toast } from "sonner";
import { IMPACT_SEVERITIES, ImpactAssessment, ImpactSeverity, listImpacts, setImpact } from "../../api";
import { describeError } from "../../messages";
import { humanize, recordText } from "./case-format";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

interface Props {
  caseId: string;
  // The case is closed. Assessments cannot be changed, so the assess buttons are not offered.
  closed: boolean;
  // The caller has cases:triage. Without it the impacts are listed read-only.
  canWrite: boolean;
  // The caller has cases:read_all. Without it the scope text is not returned.
  canReadFullView: boolean;
  // Called after an assessment is stored. The caller reloads the history, because the change is audited.
  onRecorded: () => void;
}

const SCOPE_MAX_LENGTH = 1000;

export function CaseImpacts({ caseId, closed, canWrite, canReadFullView, onRecorded }: Props) {
  const [items, setItems] = useState<ImpactAssessment[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The area being assessed. Null while the dialog is closed.
  const [editing, setEditing] = useState<ImpactAssessment | null>(null);
  const canAssess = canWrite && !closed;

  useEffect(() => {
    let stale = false;
    setError(null);
    listImpacts(caseId)
      .then((result) => {
        if (!stale) setItems(result);
      })
      .catch((caught: unknown) => {
        if (!stale) setError(describeError(caught));
      });
    return () => {
      stale = true;
    };
  }, [caseId]);

  // The server returns the saved assessment. It replaces the row, so the table shows what was stored.
  function replaceRow(saved: ImpactAssessment) {
    setItems((current) => current?.map((row) => (row.area === saved.area ? saved : row)) ?? null);
  }

  let description = "Impact on people, in every area the case must address.";
  if (closed) description = "This case is closed, so its impact assessment cannot be changed.";
  else if (!canWrite) description = "Impact assessment of this case. Your account cannot change it.";

  return (
    <Card>
      <CardHeader>
        <CardTitle>Impacts</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {items === null && error === null && <p className="text-sm text-muted-foreground">Loading impacts…</p>}
        {items !== null && (
          <div className="overflow-hidden rounded-md border">
            <Table>
              <TableHeader className="bg-muted">
                <TableRow>
                  <TableHead>Area</TableHead>
                  <TableHead>Assessment</TableHead>
                  <TableHead>Scope</TableHead>
                  {canAssess && <TableHead>Actions</TableHead>}
                </TableRow>
              </TableHeader>
              <TableBody>
                {items.map((impact) => (
                  <TableRow key={impact.area}>
                    <TableCell>{humanize(impact.area)}</TableCell>
                    <TableCell>
                      {impact.assessed && impact.severity !== null ? (
                        <Badge variant="secondary">{humanize(impact.severity)}</Badge>
                      ) : (
                        // Not the same as severity "none": nothing has been assessed yet.
                        <Badge variant="outline">Not assessed</Badge>
                      )}
                    </TableCell>
                    <TableCell className="whitespace-normal text-muted-foreground">
                      {impact.assessed ? recordText(impact.scope, canReadFullView) : "—"}
                    </TableCell>
                    {canAssess && (
                      <TableCell>
                        <Button size="sm" variant="outline" onClick={() => setEditing(impact)}>
                          {impact.assessed ? "Update" : "Assess"}
                        </Button>
                      </TableCell>
                    )}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>
      {canAssess && (
        <AssessImpactDialog
          caseId={caseId}
          impact={editing}
          onOpenChange={(open) => {
            if (!open) setEditing(null);
          }}
          onSaved={(saved) => {
            replaceRow(saved);
            setEditing(null);
            onRecorded();
          }}
        />
      )}
    </Card>
  );
}

interface DialogProps {
  caseId: string;
  // The row being assessed. The dialog is open while this is set.
  impact: ImpactAssessment | null;
  onOpenChange: (open: boolean) => void;
  onSaved: (saved: ImpactAssessment) => void;
}

function AssessImpactDialog({ caseId, impact, onOpenChange, onSaved }: DialogProps) {
  return (
    <Dialog open={impact !== null} onOpenChange={onOpenChange}>
      <DialogContent>
        {/* The form is mounted only while an area is open, so every opening starts from that area's row. */}
        {impact !== null && <AssessImpactForm key={impact.area} caseId={caseId} impact={impact} onSaved={onSaved} />}
      </DialogContent>
    </Dialog>
  );
}

function AssessImpactForm({
  caseId,
  impact,
  onSaved,
}: {
  caseId: string;
  impact: ImpactAssessment;
  onSaved: (saved: ImpactAssessment) => void;
}) {
  // A scope the caller cannot read starts empty. The save replaces it, so the caller must type it again.
  const [severity, setSeverity] = useState<ImpactSeverity | null>(impact.assessed ? impact.severity : null);
  const [scope, setScope] = useState(impact.assessed ? (impact.scope ?? "") : "");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (severity === null) {
      setError("Choose the severity for this area. Choose Unknown if the answer is not known yet.");
      return;
    }
    const trimmed = scope.trim();
    if (trimmed.length === 0) {
      setError("Enter the scope for this area.");
      return;
    }
    setBusy(true);
    try {
      const saved = await setImpact(caseId, impact.area, { severity, scope: trimmed });
      toast.success(`${humanize(impact.area)} assessment saved.`);
      onSaved(saved);
    } catch (caught) {
      setError(describeError(caught));
      setBusy(false);
    }
  }

  return (
    <>
      <DialogHeader>
        <DialogTitle>{humanize(impact.area)}</DialogTitle>
        <DialogDescription>
          Choose the severity and describe the scope. Choose Unknown when the answer is not known yet; it is recorded as an
          assessed area.
        </DialogDescription>
      </DialogHeader>
      <form onSubmit={handleSubmit} className="flex flex-col gap-4">
        <FieldGroup>
          <Field>
            <FieldLabel htmlFor="impact-severity">Severity</FieldLabel>
            <Select
              value={severity}
              onValueChange={(value) => {
                if (value) setSeverity(value as ImpactSeverity);
              }}
            >
              <SelectTrigger id="impact-severity" className="w-full">
                <SelectValue>
                  {(value: ImpactSeverity | null) => (value ? humanize(value) : "Choose a severity")}
                </SelectValue>
              </SelectTrigger>
              <SelectContent>
                {IMPACT_SEVERITIES.map((item) => (
                  <SelectItem key={item} value={item}>
                    {humanize(item)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          <Field>
            <FieldLabel htmlFor="impact-scope">Scope</FieldLabel>
            <Textarea
              id="impact-scope"
              value={scope}
              maxLength={SCOPE_MAX_LENGTH}
              onChange={(event) => setScope(event.target.value)}
              rows={4}
            />
          </Field>
        </FieldGroup>
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        <DialogFooter>
          <Button type="submit" disabled={busy}>
            {busy ? "Saving…" : "Save assessment"}
          </Button>
        </DialogFooter>
      </form>
    </>
  );
}
