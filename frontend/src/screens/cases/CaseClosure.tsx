import { FormEvent, useState } from "react";
import { toast } from "sonner";
import { CaseDetail, closeCase } from "../../api";
import { describeError } from "../../messages";
import { ReopenCaseDialog } from "./ReopenCaseDialog";
import { Timestamp } from "@/components/timestamp";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Textarea } from "@/components/ui/textarea";

interface Props {
  caseId: string;
  status: string;
  closedAt: string | null;
  closeReason: string | null;
  // Reason of the latest reopen. Null when the case has never been reopened.
  reopenReason: string | null;
  // The caller has cases:close. Without it the close form is replaced by a note, and reopen is not offered.
  canClose: boolean;
  onChanged: (detail: CaseDetail) => void;
}

const REASON_MAX_LENGTH = 1000;

export function CaseClosure({ caseId, status, closedAt, closeReason, reopenReason, canClose, onChanged }: Props) {
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  // The field the current error is about, so that the field can point at the message.
  const [errorField, setErrorField] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [reopenOpen, setReopenOpen] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setErrorField(null);
    const trimmed = reason.trim();
    if (trimmed.length === 0) {
      setErrorField("close-reason");
      setError("Enter the reason for closing the case.");
      return;
    }
    setBusy(true);
    try {
      const closed = await closeCase(caseId, trimmed);
      toast.success("Case closed.");
      onChanged(closed);
    } catch (caught) {
      // The server lists what is still missing, for example a review or an open deadline.
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  const reopenNote = reopenReason ? (
    <dl className="grid gap-1 text-sm">
      <dt className="font-medium">Reopen reason</dt>
      <dd className="whitespace-pre-wrap text-muted-foreground">{reopenReason}</dd>
    </dl>
  ) : null;

  if (status === "closed") {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Closed</CardTitle>
          <CardDescription>This case is closed. Its deadlines and assignments are locked.</CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <dl className="grid gap-4 text-sm sm:grid-cols-2">
            <div className="grid gap-1">
              <dt className="font-medium">Closed at</dt>
              <dd className="text-muted-foreground">{closedAt ? <Timestamp value={closedAt} /> : "Unknown"}</dd>
            </div>
            <div className="grid gap-1">
              <dt className="font-medium">Reason</dt>
              <dd className="whitespace-pre-wrap text-muted-foreground">{closeReason ?? "No reason recorded."}</dd>
            </div>
          </dl>
          {reopenNote}
          {canClose && (
            <div>
              <Button variant="outline" onClick={() => setReopenOpen(true)}>
                Reopen case
              </Button>
            </div>
          )}
          {canClose && (
            <ReopenCaseDialog
              caseId={caseId}
              open={reopenOpen}
              onOpenChange={setReopenOpen}
              onReopened={(detail) => onChanged(detail)}
            />
          )}
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Close case</CardTitle>
        <CardDescription>
          {canClose
            ? "A case needs at least one review, and every deadline must be done or not applicable."
            : "Your account cannot close cases."}
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {reopenNote}
        {canClose && (
          // noValidate: the empty reason is reported by handleSubmit, where the message is announced and linked to the field.
          <form onSubmit={handleSubmit} noValidate className="flex flex-col gap-4">
            <FieldGroup>
              <Field>
                <FieldLabel htmlFor="close-reason">
                  Reason <span className="font-normal text-muted-foreground">(required)</span>
                </FieldLabel>
                <Textarea
                  id="close-reason"
                  aria-invalid={errorField === "close-reason" || undefined}
                  aria-describedby={errorField === "close-reason" ? "close-error" : undefined}
                  value={reason}
                  maxLength={REASON_MAX_LENGTH}
                  onChange={(event) => setReason(event.target.value)}
                  rows={3}
                  required
                />
                <FieldDescription>Stored with the case and shown after closure.</FieldDescription>
              </Field>
            </FieldGroup>
            {error && (
              <Alert variant="destructive" id="close-error">
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            )}
            <div>
              <Button type="submit" disabled={busy}>
                {busy ? "Closing…" : "Close case"}
              </Button>
            </div>
          </form>
        )}
      </CardContent>
    </Card>
  );
}
