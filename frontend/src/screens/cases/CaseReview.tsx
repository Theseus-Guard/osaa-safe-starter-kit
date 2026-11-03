import { FormEvent, useEffect, useState } from "react";
import { toast } from "sonner";
import {
  ApiError,
  LayerOutcome,
  REVIEW_LAYERS,
  ReviewLayer,
  ReviewRound,
  listReviewRounds,
  openReviewRound,
  setReviewLayer,
} from "../../api";
import { describeError } from "../../messages";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";
import { ReviewIndependence } from "./ReviewIndependence";

interface Props {
  caseId: string;
  // The case is closed. No round can be opened and no layer can change, so the forms are not offered.
  closed: boolean;
  // The caller has reviews:write. Without it the rounds are shown read-only.
  canReview: boolean;
  // The caller may file an appeal. Without it the appeal form is not offered (the disclosure approver).
  canAppeal?: boolean;
  // Called after a round is opened or a layer is saved. The caller reloads the history, because both are audited.
  onRecorded: () => void;
}

const REASON_LIMIT = 1000;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

const OUTCOMES: { value: LayerOutcome; label: string }[] = [
  { value: "pass", label: "Pass" },
  { value: "fail", label: "Fail" },
  { value: "insufficient_evidence", label: "Insufficient evidence" },
  { value: "not_applicable", label: "Not applicable" },
];

function outcomeLabel(value: LayerOutcome | null): string {
  if (value === null) return "Not set";
  return OUTCOMES.find((outcome) => outcome.value === value)?.label ?? value;
}

function layerLabel(key: string): string {
  return REVIEW_LAYERS.find((item) => item.key === key)?.label ?? key;
}

// The conflict message after a stale save. The save was refused, so the user checks the layers and saves again.
function conflictMessage(current: number | null): string {
  const revision = current === null ? "" : ` The current revision is ${current}.`;
  return `The review was changed by someone else, so nothing was saved.${revision} Check the layers and save again.`;
}

interface ActiveLayer {
  round: ReviewRound;
  layer: ReviewLayer;
}

export function CaseReview({ caseId, closed, canReview, canAppeal, onRecorded }: Props) {
  const [rounds, setRounds] = useState<ReviewRound[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [conflict, setConflict] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [active, setActive] = useState<ActiveLayer | null>(null);
  // Bumped after a change so the rounds reload from the server.
  const [version, setVersion] = useState(0);
  const canRecord = canReview && !closed;

  useEffect(() => {
    let stale = false;
    setError(null);
    listReviewRounds(caseId)
      .then((result) => {
        if (!stale) setRounds(result);
      })
      .catch((caught: unknown) => {
        if (!stale) setError(describeError(caught));
      });
    return () => {
      stale = true;
    };
  }, [caseId, version]);

  async function handleOpen() {
    setError(null);
    setConflict(null);
    setBusy(true);
    try {
      await openReviewRound(caseId);
      toast.success("Review round opened.");
      setVersion((value) => value + 1);
      onRecorded();
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  let description = "The review of this case, layer by layer.";
  if (canReview) {
    description = closed
      ? "This case is closed, so no review can be recorded."
      : "Open a review round, then set the outcome and reason of each layer. Only an assigned reviewer can change a layer.";
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
        {conflict && (
          <Alert variant="destructive">
            <AlertDescription>{conflict}</AlertDescription>
          </Alert>
        )}
        {rounds === null && error === null && <p className="text-sm text-muted-foreground">Loading reviews…</p>}
        {rounds !== null && rounds.length === 0 && (
          <p className="text-sm text-muted-foreground">No review round has been opened for this case yet.</p>
        )}
        {rounds !== null &&
          rounds.map((round) => (
            <div key={round.id} className="grid gap-4">
              <RoundView
                round={round}
                canSet={canRecord && round.status === "open"}
                onSet={(layer) => setActive({ round, layer })}
              />
              <ReviewIndependence
                roundId={round.id}
                canDeclare={canRecord && round.status === "open"}
                canAppeal={canAppeal}
              />
            </div>
          ))}
        {canRecord && (
          <div>
            <Button variant="outline" onClick={handleOpen} disabled={busy}>
              {busy ? "Opening…" : "Open review round"}
            </Button>
          </div>
        )}
        {canRecord && (
          <Dialog open={active !== null} onOpenChange={(open) => !open && setActive(null)}>
            <DialogContent>
              {/* The form is mounted only while the dialog is open, so every opening starts empty. */}
              {active !== null && (
                <LayerForm
                  key={`${active.round.id}-${active.layer.layer}`}
                  active={active}
                  onSaved={() => {
                    setActive(null);
                    setConflict(null);
                    setVersion((value) => value + 1);
                    onRecorded();
                  }}
                  onConflict={(current) => {
                    setActive(null);
                    setConflict(conflictMessage(current));
                    setVersion((value) => value + 1);
                  }}
                />
              )}
            </DialogContent>
          </Dialog>
        )}
      </CardContent>
    </Card>
  );
}

function RoundView({
  round,
  canSet,
  onSet,
}: {
  round: ReviewRound;
  canSet: boolean;
  onSet: (layer: ReviewLayer) => void;
}) {
  return (
    <section className="grid gap-2">
      <p className="text-sm text-muted-foreground">
        Revision {round.revision}, {round.status === "open" ? "open" : "closed"}
      </p>
      <ul className="grid gap-3 text-sm">
        {round.layers.map((layer) => (
          <li key={layer.layer} className="grid gap-1 border-b pb-2 last:border-b-0">
            <div className="flex items-center justify-between gap-2">
              <span className="font-medium">{layerLabel(layer.layer)}</span>
              {canSet && (
                <Button variant="ghost" size="sm" aria-label={`Set ${layerLabel(layer.layer)}`} onClick={() => onSet(layer)}>
                  Set
                </Button>
              )}
            </div>
            <span>{outcomeLabel(layer.outcome)}</span>
            {layer.reason && <span className="whitespace-pre-wrap text-muted-foreground">{layer.reason}</span>}
          </li>
        ))}
      </ul>
    </section>
  );
}

function parseEvidenceIds(text: string): string[] | null {
  const ids = text
    .split(/[\s,]+/)
    .map((item) => item.trim())
    .filter((item) => item.length > 0);
  return ids.every((id) => UUID_PATTERN.test(id)) ? ids : null;
}

function LayerForm({
  active,
  onSaved,
  onConflict,
}: {
  active: ActiveLayer;
  onSaved: () => void;
  onConflict: (current: number | null) => void;
}) {
  const [outcome, setOutcome] = useState<LayerOutcome | null>(active.layer.outcome);
  const [reason, setReason] = useState(active.layer.reason ?? "");
  const [evidence, setEvidence] = useState(active.layer.evidence_ids.join(", "));
  const [error, setError] = useState<string | null>(null);
  // The field the current error is about, so that the field can point at the message.
  const [errorField, setErrorField] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const label = layerLabel(active.layer.layer);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setErrorField(null);
    if (outcome === null) {
      setErrorField("review-outcome");
      setError("Choose the outcome of this layer.");
      return;
    }
    const trimmed = reason.trim();
    // A reason is required for every outcome except pass. The check runs before anything is sent.
    if (outcome !== "pass" && trimmed.length === 0) {
      setError("A reason is required for this outcome.");
      return;
    }
    const evidenceIds = parseEvidenceIds(evidence);
    if (evidenceIds === null) {
      setError("Evidence IDs must be item IDs separated by commas or spaces.");
      return;
    }
    // A pass is supported only by evidence of the case (T06). The backend refuses it too; this stops it before sending.
    if (outcome === "pass" && evidenceIds.length === 0) {
      setError("A pass needs at least one evidence item of this case.");
      return;
    }
    setBusy(true);
    try {
      await setReviewLayer(
        active.round.id,
        active.layer.layer,
        {
          outcome,
          reason: trimmed.length > 0 ? trimmed : null,
          evidence_ids: evidenceIds,
        },
        active.round.revision,
      );
      toast.success("Review layer saved.");
      onSaved();
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === "precondition_failed") {
        onConflict(caught.currentRevision);
        return;
      }
      setError(describeError(caught));
      setBusy(false);
    }
  }

  return (
    <>
      <DialogHeader>
        <DialogTitle>{label}</DialogTitle>
        <DialogDescription>The outcome and reason are stored with this review round.</DialogDescription>
      </DialogHeader>
      <form onSubmit={handleSubmit} className="flex flex-col gap-4">
        <FieldGroup>
          <Field>
            <FieldLabel htmlFor="review-layer-outcome">
              Outcome <span className="font-normal text-muted-foreground">(required)</span>
            </FieldLabel>
            <Select
              value={outcome}
              onValueChange={(value) => {
                if (value) setOutcome(value as LayerOutcome);
              }}
            >
              <SelectTrigger
                id="review-layer-outcome"
                className="w-full"
                aria-invalid={errorField === "review-outcome" || undefined}
                aria-describedby={errorField === "review-outcome" ? "review-error" : undefined}
              >
                <SelectValue>{(value: LayerOutcome | null) => (value ? outcomeLabel(value) : "Choose an outcome")}</SelectValue>
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
            <FieldLabel htmlFor="review-layer-reason">Reason</FieldLabel>
            <Textarea
              id="review-layer-reason"
              value={reason}
              maxLength={REASON_LIMIT}
              onChange={(event) => setReason(event.target.value)}
              rows={3}
            />
            <FieldDescription>Required for every outcome except pass.</FieldDescription>
          </Field>
          <Field>
            <FieldLabel htmlFor="review-layer-evidence">Evidence IDs</FieldLabel>
            <Input id="review-layer-evidence" value={evidence} onChange={(event) => setEvidence(event.target.value)} />
            <FieldDescription>
              Item IDs of this case, separated by commas or spaces. Required for a pass; optional for the other outcomes.
            </FieldDescription>
          </Field>
        </FieldGroup>
        {error && (
          <Alert variant="destructive" id="review-error">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        <DialogFooter>
          <Button type="submit" disabled={busy}>
            {busy ? "Saving…" : "Save layer"}
          </Button>
        </DialogFooter>
      </form>
    </>
  );
}
