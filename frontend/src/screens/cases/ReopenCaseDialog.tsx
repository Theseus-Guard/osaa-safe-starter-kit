import { FormEvent, useState } from "react";
import { toast } from "sonner";
import { CaseDetail, reopenCase } from "../../api";
import { describeError } from "../../messages";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";

const REASON_MAX_LENGTH = 1000;

interface Props {
  caseId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onReopened: (detail: CaseDetail) => void;
}

export function ReopenCaseDialog({ caseId, open, onOpenChange, onReopened }: Props) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        {/* The form is mounted only while the dialog is open, so every opening starts with an empty reason. */}
        <ReopenCaseForm
          caseId={caseId}
          onReopened={(detail) => {
            onOpenChange(false);
            onReopened(detail);
          }}
        />
      </DialogContent>
    </Dialog>
  );
}

function ReopenCaseForm({ caseId, onReopened }: { caseId: string; onReopened: (detail: CaseDetail) => void }) {
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  // The field the current error is about, so that the field can point at the message.
  const [errorField, setErrorField] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setErrorField(null);
    const trimmed = reason.trim();
    if (trimmed.length === 0) {
      setErrorField("reopen-reason");
      setError("Enter the reason for reopening the case.");
      return;
    }
    setBusy(true);
    try {
      const reopened = await reopenCase(caseId, trimmed);
      toast.success("Case reopened.");
      onReopened(reopened);
    } catch (caught) {
      setError(describeError(caught));
      setBusy(false);
    }
  }

  return (
    <>
      <DialogHeader>
        <DialogTitle>Reopen case</DialogTitle>
        <DialogDescription>Reopening unlocks the deadlines and assignments. The reason is stored with the case.</DialogDescription>
      </DialogHeader>
      <form onSubmit={handleSubmit} className="flex flex-col gap-4">
        <FieldGroup>
          <Field>
            <FieldLabel htmlFor="reopen-reason">
              Reason <span className="font-normal text-muted-foreground">(required)</span>
            </FieldLabel>
            <Textarea
              id="reopen-reason"
              aria-invalid={errorField === "reopen-reason" || undefined}
              aria-describedby={errorField === "reopen-reason" ? "reopen-error" : undefined}
              value={reason}
              maxLength={REASON_MAX_LENGTH}
              onChange={(event) => setReason(event.target.value)}
              rows={3}
              required
            />
            <FieldDescription>Why the case must be worked on again. Shown in the history.</FieldDescription>
          </Field>
        </FieldGroup>
        {error && (
          <Alert variant="destructive" id="reopen-error">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        <DialogFooter>
          <Button type="submit" disabled={busy}>
            {busy ? "Reopening…" : "Reopen case"}
          </Button>
        </DialogFooter>
      </form>
    </>
  );
}
