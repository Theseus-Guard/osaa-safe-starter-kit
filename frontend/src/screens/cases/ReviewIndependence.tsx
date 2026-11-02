import { FormEvent, useEffect, useId, useState } from "react";
import { toast } from "sonner";
import {
  ReviewAppeal,
  ReviewConflict,
  ReviewRecusal,
  declareReviewConflict,
  fileReviewAppeal,
  listReviewAppeals,
  listReviewConflicts,
  listReviewRecusals,
  recuseFromReviewRound,
} from "../../api";
import { describeError } from "../../messages";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

const TEXT_LIMIT = 1000;
const GROUNDS_LIMIT = 2000;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
// Shown for free text the API hides from this caller (it is null for callers without cases:read_all).
const NOT_VISIBLE_TEXT = "(not visible to your account)";

interface Props {
  roundId: string;
  // The caller may declare a conflict or withdraw from the round: reviews:write, an open case, and an open round.
  // Without it the conflict and recusal forms are not offered. Appeals need only read access, so their form is always
  // offered.
  canDeclare: boolean;
}

interface Independence {
  conflicts: ReviewConflict[];
  recusals: ReviewRecusal[];
  appeals: ReviewAppeal[];
}

// The conflict, recusal and appeal records of one review round, with the forms to add them. Nothing here is edited:
// a correction is a new entry, and the lists show every entry, the current one first.
export function ReviewIndependence({ roundId, canDeclare }: Props) {
  const [loaded, setLoaded] = useState<Independence | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Bumped after a change so the lists reload from the server.
  const [version, setVersion] = useState(0);

  useEffect(() => {
    let stale = false;
    setError(null);
    Promise.all([listReviewConflicts(roundId), listReviewRecusals(roundId), listReviewAppeals(roundId)])
      .then(([conflicts, recusals, appeals]) => {
        if (!stale) setLoaded({ conflicts, recusals, appeals });
      })
      .catch((caught: unknown) => {
        if (!stale) setError(describeError(caught));
      });
    return () => {
      stale = true;
    };
  }, [roundId, version]);

  const reload = () => setVersion((value) => value + 1);

  return (
    <section aria-label="Conflicts, recusal and appeals" className="grid gap-4 border-t pt-4">
      <h3 className="text-sm font-medium">Conflicts, recusal and appeals</h3>
      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      {loaded === null && error === null && <p className="text-sm text-muted-foreground">Loading…</p>}
      {loaded !== null && <IndependenceLists data={loaded} />}
      {canDeclare && <ConflictForm roundId={roundId} onSaved={reload} />}
      {canDeclare && <RecusalForm roundId={roundId} onSaved={reload} />}
      <AppealForm roundId={roundId} onSaved={reload} />
    </section>
  );
}

function IndependenceLists({ data }: { data: Independence }) {
  return (
    <div className="grid gap-3 text-sm">
      <div className="grid gap-1">
        <span className="font-medium">Conflicts</span>
        {data.conflicts.length === 0 && <p className="text-muted-foreground">No conflict is recorded on this round.</p>}
        {data.conflicts.map((entry) => (
          <div key={entry.id} className="grid gap-0.5">
            <span>
              {entry.kind === "declared" ? "Conflict declared" : "Conflict withdrawn"}
              {entry.current ? "" : " (superseded)"}
            </span>
            <span className="whitespace-pre-wrap text-muted-foreground">{entry.description ?? NOT_VISIBLE_TEXT}</span>
          </div>
        ))}
      </div>
      <div className="grid gap-1">
        <span className="font-medium">Recusals</span>
        {data.recusals.length === 0 && <p className="text-muted-foreground">No reviewer has withdrawn from this round.</p>}
        {data.recusals.map((entry) => (
          <div key={entry.id} className="grid gap-0.5">
            <span>
              Withdrawn. Replacement: <span className="font-mono text-xs">{entry.replacement_id}</span>
              {entry.current ? "" : " (superseded)"}
            </span>
            <span className="whitespace-pre-wrap text-muted-foreground">{entry.reason ?? NOT_VISIBLE_TEXT}</span>
          </div>
        ))}
      </div>
      <div className="grid gap-1">
        <span className="font-medium">Appeals</span>
        {data.appeals.length === 0 && <p className="text-muted-foreground">No appeal has been filed on this round.</p>}
        {data.appeals.map((entry) => (
          <div key={entry.id} className="grid gap-0.5">
            <span>
              Appeal for independent reviewer <span className="font-mono text-xs">{entry.independent_reviewer_id}</span>.
              The product does not decide it.
            </span>
            <span className="whitespace-pre-wrap text-muted-foreground">{entry.grounds ?? NOT_VISIBLE_TEXT}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function ConflictForm({ roundId, onSaved }: { roundId: string; onSaved: () => void }) {
  const fieldId = useId();
  const [description, setDescription] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    const trimmed = description.trim();
    if (trimmed.length === 0) {
      setError("Describe the conflict before you declare it.");
      return;
    }
    setBusy(true);
    try {
      await declareReviewConflict(roundId, { kind: "declared", description: trimmed });
      toast.success("Conflict declared.");
      setDescription("");
      onSaved();
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} aria-label="Declare a conflict" className="grid gap-3">
      <FieldGroup>
        <Field>
          <FieldLabel htmlFor={`${fieldId}-description`}>Conflict</FieldLabel>
          <Textarea
            id={`${fieldId}-description`}
            value={description}
            maxLength={TEXT_LIMIT}
            rows={2}
            onChange={(event) => setDescription(event.target.value)}
          />
          <FieldDescription>Say what the conflict is. You cannot set the layers of this round once you declare it.</FieldDescription>
        </Field>
      </FieldGroup>
      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      <div>
        <Button type="submit" variant="outline" disabled={busy}>
          {busy ? "Declaring…" : "Declare conflict"}
        </Button>
      </div>
    </form>
  );
}

function RecusalForm({ roundId, onSaved }: { roundId: string; onSaved: () => void }) {
  const fieldId = useId();
  const [replacement, setReplacement] = useState("");
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    const replacementId = replacement.trim();
    if (!UUID_PATTERN.test(replacementId)) {
      setError("Enter the user ID (UUID) of the replacement reviewer.");
      return;
    }
    const trimmedReason = reason.trim();
    setBusy(true);
    try {
      await recuseFromReviewRound(roundId, {
        replacement_id: replacementId,
        reason: trimmedReason.length > 0 ? trimmedReason : null,
      });
      toast.success("Recusal recorded.");
      setReplacement("");
      setReason("");
      onSaved();
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} aria-label="Withdraw from this round" className="grid gap-3">
      <FieldGroup>
        <Field>
          <FieldLabel htmlFor={`${fieldId}-replacement`}>Replacement reviewer ID</FieldLabel>
          <Input
            id={`${fieldId}-replacement`}
            value={replacement}
            onChange={(event) => setReplacement(event.target.value)}
          />
          <FieldDescription>
            The user ID of an assigned reviewer with no declared conflict on this round. Required.
          </FieldDescription>
        </Field>
        <Field>
          <FieldLabel htmlFor={`${fieldId}-recusal-reason`}>Recusal reason</FieldLabel>
          <Textarea
            id={`${fieldId}-recusal-reason`}
            value={reason}
            maxLength={TEXT_LIMIT}
            rows={2}
            onChange={(event) => setReason(event.target.value)}
          />
        </Field>
      </FieldGroup>
      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      <div>
        <Button type="submit" variant="outline" disabled={busy}>
          {busy ? "Withdrawing…" : "Withdraw and name replacement"}
        </Button>
      </div>
    </form>
  );
}

function AppealForm({ roundId, onSaved }: { roundId: string; onSaved: () => void }) {
  const fieldId = useId();
  const [grounds, setGrounds] = useState("");
  const [independent, setIndependent] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    const trimmedGrounds = grounds.trim();
    if (trimmedGrounds.length === 0) {
      setError("Give the grounds of the appeal.");
      return;
    }
    const independentId = independent.trim();
    if (!UUID_PATTERN.test(independentId)) {
      setError("Enter the user ID (UUID) of the independent reviewer.");
      return;
    }
    setBusy(true);
    try {
      await fileReviewAppeal(roundId, { grounds: trimmedGrounds, independent_reviewer_id: independentId });
      toast.success("Appeal filed.");
      setGrounds("");
      setIndependent("");
      onSaved();
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} aria-label="File an appeal" className="grid gap-3">
      <FieldGroup>
        <Field>
          <FieldLabel htmlFor={`${fieldId}-grounds`}>Appeal grounds</FieldLabel>
          <Textarea
            id={`${fieldId}-grounds`}
            value={grounds}
            maxLength={GROUNDS_LIMIT}
            rows={2}
            onChange={(event) => setGrounds(event.target.value)}
          />
        </Field>
        <Field>
          <FieldLabel htmlFor={`${fieldId}-independent`}>Independent reviewer ID</FieldLabel>
          <Input
            id={`${fieldId}-independent`}
            value={independent}
            onChange={(event) => setIndependent(event.target.value)}
          />
          <FieldDescription>
            The user ID of an assigned reviewer who did not take part in this round. The appeal needs no approval.
          </FieldDescription>
        </Field>
      </FieldGroup>
      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      <div>
        <Button type="submit" variant="outline" disabled={busy}>
          {busy ? "Filing…" : "File appeal"}
        </Button>
      </div>
    </form>
  );
}
