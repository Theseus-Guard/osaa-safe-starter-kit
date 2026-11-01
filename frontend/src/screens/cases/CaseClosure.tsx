import { FormEvent, useState } from "react";
import { toast } from "sonner";
import { CaseDetail, closeCase } from "../../api";
import { describeError } from "../../messages";
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
  onChanged: (detail: CaseDetail) => void;
}

const REASON_MAX_LENGTH = 1000;

export function CaseClosure({ caseId, status, closedAt, closeReason, onChanged }: Props) {
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    const trimmed = reason.trim();
    if (trimmed.length === 0) {
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

  if (status === "closed") {
    return (
      <Card>
        <CardHeader>
          <CardTitle>Closed</CardTitle>
          <CardDescription>This case is closed. Its deadlines and assignments are locked.</CardDescription>
        </CardHeader>
        <CardContent>
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
        </CardContent>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Close case</CardTitle>
        <CardDescription>A case needs at least one review, and every deadline must be done or not applicable.</CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <FieldGroup>
            <Field>
              <FieldLabel htmlFor="close-reason">Reason</FieldLabel>
              <Textarea
                id="close-reason"
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
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
          <div>
            <Button type="submit" disabled={busy}>
              {busy ? "Closing…" : "Close case"}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}
