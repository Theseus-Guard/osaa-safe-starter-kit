import { FormEvent, useEffect, useState } from "react";
import { toast } from "sonner";
import { ReviewOutcome, ReviewResponse, createReview, listReviews } from "../../api";
import { describeError } from "../../messages";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";

interface Props {
  caseId: string;
  // The case is closed. No review can be recorded, so the form is not offered.
  closed: boolean;
  // The caller has reviews:write. Without it the reviews are listed read-only.
  canReview: boolean;
  // Called after a review is stored. The caller reloads the history, because the review is audited.
  onRecorded: () => void;
}

const NOTE_MAX_LENGTH = 1000;

const OUTCOMES: { value: ReviewOutcome; label: string }[] = [
  { value: "pass", label: "Pass" },
  { value: "fail", label: "Fail" },
  { value: "insufficient_evidence", label: "Insufficient evidence" },
  { value: "not_applicable", label: "Not applicable" },
];

function outcomeLabel(value: ReviewOutcome): string {
  return OUTCOMES.find((outcome) => outcome.value === value)?.label ?? value;
}

export function CaseReview({ caseId, closed, canReview, onRecorded }: Props) {
  const [open, setOpen] = useState(false);
  const [items, setItems] = useState<ReviewResponse[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Bumped after a review is recorded so the list reloads from the server.
  const [version, setVersion] = useState(0);
  const canRecord = canReview && !closed;

  useEffect(() => {
    let stale = false;
    setError(null);
    listReviews(caseId)
      .then((result) => {
        if (!stale) setItems(result);
      })
      .catch((caught: unknown) => {
        if (!stale) setError(describeError(caught));
      });
    return () => {
      stale = true;
    };
  }, [caseId, version]);

  let description = "Reviews recorded on this case.";
  if (canReview) {
    description = closed
      ? "This case is closed, so no review can be recorded."
      : "Record your review of this case. Only an assigned reviewer can record one.";
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Review</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {items === null && error === null && <p className="text-sm text-muted-foreground">Loading reviews…</p>}
        {items !== null && items.length === 0 && (
          <p className="text-sm text-muted-foreground">No review has been recorded for this case yet.</p>
        )}
        {items !== null && items.length > 0 && (
          <ul className="grid gap-3 text-sm">
            {items.map((review) => (
              <li key={review.id} className="grid gap-1">
                <span>{outcomeLabel(review.outcome)}</span>
                {review.note && <span className="whitespace-pre-wrap text-muted-foreground">{review.note}</span>}
              </li>
            ))}
          </ul>
        )}
        {canRecord && (
          <div>
            <Button variant="outline" onClick={() => setOpen(true)}>
              Record review
            </Button>
          </div>
        )}
        {canRecord && (
          <ReviewCaseDialog
            caseId={caseId}
            open={open}
            onOpenChange={setOpen}
            onRecorded={() => {
              setVersion((value) => value + 1);
              onRecorded();
            }}
          />
        )}
      </CardContent>
    </Card>
  );
}

interface DialogProps {
  caseId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onRecorded: () => void;
}

function ReviewCaseDialog({ caseId, open, onOpenChange, onRecorded }: DialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        {/* The form is mounted only while the dialog is open, so every opening starts empty. */}
        <ReviewCaseForm
          caseId={caseId}
          onRecorded={() => {
            onOpenChange(false);
            onRecorded();
          }}
        />
      </DialogContent>
    </Dialog>
  );
}

function ReviewCaseForm({ caseId, onRecorded }: { caseId: string; onRecorded: () => void }) {
  const [outcome, setOutcome] = useState<ReviewOutcome | null>(null);
  const [note, setNote] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (outcome === null) {
      setError("Choose the outcome of the review.");
      return;
    }
    const trimmed = note.trim();
    setBusy(true);
    try {
      await createReview(caseId, { outcome, note: trimmed.length > 0 ? trimmed : null });
      toast.success("Review recorded.");
      onRecorded();
    } catch (caught) {
      setError(describeError(caught));
      setBusy(false);
    }
  }

  return (
    <>
      <DialogHeader>
        <DialogTitle>Record review</DialogTitle>
        <DialogDescription>The review is stored with the case and appears in the history.</DialogDescription>
      </DialogHeader>
      <form onSubmit={handleSubmit} className="flex flex-col gap-4">
        <FieldGroup>
          <Field>
            <FieldLabel htmlFor="review-outcome">Outcome</FieldLabel>
            <Select
              value={outcome}
              onValueChange={(value) => {
                if (value) setOutcome(value as ReviewOutcome);
              }}
            >
              <SelectTrigger id="review-outcome" className="w-full">
                <SelectValue>
                  {(value: ReviewOutcome | null) => (value ? outcomeLabel(value) : "Choose an outcome")}
                </SelectValue>
              </SelectTrigger>
              <SelectContent>
                {OUTCOMES.map((item) => (
                  <SelectItem key={item.value} value={item.value}>
                    {item.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          <Field>
            <FieldLabel htmlFor="review-note">Note</FieldLabel>
            <Textarea
              id="review-note"
              value={note}
              maxLength={NOTE_MAX_LENGTH}
              onChange={(event) => setNote(event.target.value)}
              rows={3}
            />
            <FieldDescription>Optional. Stored with the review.</FieldDescription>
          </Field>
        </FieldGroup>
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        <DialogFooter>
          <Button type="submit" disabled={busy}>
            {busy ? "Submitting…" : "Submit review"}
          </Button>
        </DialogFooter>
      </form>
    </>
  );
}
