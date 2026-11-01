import { FormEvent, useEffect, useState } from "react";
import { toast } from "sonner";
import {
  AWARENESS_KINDS,
  AwarenessEvent,
  AwarenessKind,
  RECORD_EVENT_KINDS,
  RecordEventKind,
  addAwarenessEvent,
  listAwarenessEvents,
} from "../../api";
import { describeError } from "../../messages";
import { useSystemTimezone } from "../../system-timezone";
import { zonedParts, zonedToUtcIso } from "../../time";
import { humanize, recordText } from "./case-format";
import { Timestamp } from "@/components/timestamp";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";

interface Props {
  caseId: string;
  // The case is closed. Records cannot be added, so the button is not offered.
  closed: boolean;
  // The caller has cases:triage. Without it the events are listed read-only.
  canWrite: boolean;
  // The caller has cases:read_all. Without it the basis text is not returned.
  canReadFullView: boolean;
  // Called after an event is stored. The caller reloads the history, because the event is audited.
  onRecorded: () => void;
}

const BASIS_MAX_LENGTH = 1000;

export function CaseAwarenessEvents({ caseId, closed, canWrite, canReadFullView, onRecorded }: Props) {
  const [items, setItems] = useState<AwarenessEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  // Bumped after an event is recorded so the list reloads from the server.
  const [version, setVersion] = useState(0);
  const canAdd = canWrite && !closed;

  useEffect(() => {
    let stale = false;
    setError(null);
    listAwarenessEvents(caseId)
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

  let description = "When and how the organization became aware of the incident. Each event keeps its own time.";
  if (closed) description = "This case is closed, so no awareness event can be recorded.";
  else if (!canWrite) description = "Awareness events recorded on this case. Your account cannot add one.";

  return (
    <Card>
      <CardHeader>
        <CardTitle>Awareness events</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {items === null && error === null && <p className="text-sm text-muted-foreground">Loading awareness events…</p>}
        {items !== null && items.length === 0 && (
          <p className="text-sm text-muted-foreground">No awareness event has been recorded for this case yet.</p>
        )}
        {items !== null && items.length > 0 && (
          <ul className="grid gap-3 text-sm">
            {items.map((event) => (
              <li key={event.id} className="grid gap-1">
                <div className="flex flex-wrap items-center gap-2">
                  <Badge variant="secondary">{humanize(event.awareness_kind)}</Badge>
                  <Badge variant="outline">{humanize(event.event_kind)}</Badge>
                  <Timestamp value={event.occurred_at} />
                </div>
                <span className="whitespace-pre-wrap text-muted-foreground">
                  {recordText(event.basis, canReadFullView)}
                </span>
              </li>
            ))}
          </ul>
        )}
        {canAdd && (
          <div>
            <Button variant="outline" onClick={() => setOpen(true)}>
              Add awareness event
            </Button>
          </div>
        )}
        {canAdd && (
          <AddAwarenessEventDialog
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

function AddAwarenessEventDialog({ caseId, open, onOpenChange, onRecorded }: DialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        {/* The form is mounted only while the dialog is open, so every opening starts empty and at the current time. */}
        <AddAwarenessEventForm
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

function AddAwarenessEventForm({ caseId, onRecorded }: { caseId: string; onRecorded: () => void }) {
  const { timezone } = useSystemTimezone();
  const zone = timezone ?? "UTC";
  const [awarenessKind, setAwarenessKind] = useState<AwarenessKind | null>(null);
  const [eventKind, setEventKind] = useState<RecordEventKind | null>(null);
  // The time is entered as a wall-clock date and time in the system timezone, and converted to UTC on submit.
  const [initial] = useState(() => zonedParts(new Date(), zone));
  const [date, setDate] = useState(initial.date);
  const [time, setTime] = useState(initial.time);
  const [basis, setBasis] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (awarenessKind === null || eventKind === null) {
      setError("Choose how the organization became aware and what kind of event it was.");
      return;
    }
    const trimmedBasis = basis.trim();
    if (trimmedBasis.length === 0) {
      setError("Enter the basis for this awareness event.");
      return;
    }
    if (date === "" || time === "") {
      setError("Enter the date and time the event occurred.");
      return;
    }
    const occurredAt = zonedToUtcIso(date, time, zone);
    if (occurredAt === null) {
      setError(`This date and time does not exist in ${zone} because of a daylight saving change. Choose another time.`);
      return;
    }
    if (new Date(occurredAt).getTime() > Date.now()) {
      setError("The time cannot be in the future. Choose the time the event occurred, not a later one.");
      return;
    }
    setBusy(true);
    try {
      await addAwarenessEvent(caseId, {
        awareness_kind: awarenessKind,
        event_kind: eventKind,
        occurred_at: occurredAt,
        basis: trimmedBasis,
      });
      toast.success("Awareness event recorded.");
      onRecorded();
    } catch (caught) {
      setError(describeError(caught));
      setBusy(false);
    }
  }

  return (
    <>
      <DialogHeader>
        <DialogTitle>Add awareness event</DialogTitle>
        <DialogDescription>Each event keeps its own time. Events cannot be edited or removed once recorded.</DialogDescription>
      </DialogHeader>
      <form onSubmit={handleSubmit} className="flex flex-col gap-4">
        <FieldGroup>
          <Field>
            <FieldLabel htmlFor="awareness-kind">Awareness through</FieldLabel>
            <Select
              value={awarenessKind}
              onValueChange={(value) => {
                if (value) setAwarenessKind(value as AwarenessKind);
              }}
            >
              <SelectTrigger id="awareness-kind" className="w-full">
                <SelectValue>
                  {(value: AwarenessKind | null) => (value ? humanize(value) : "Choose how the organization became aware")}
                </SelectValue>
              </SelectTrigger>
              <SelectContent>
                {AWARENESS_KINDS.map((kind) => (
                  <SelectItem key={kind} value={kind}>
                    {humanize(kind)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          <Field>
            <FieldLabel htmlFor="awareness-event-kind">Event</FieldLabel>
            <Select
              value={eventKind}
              onValueChange={(value) => {
                if (value) setEventKind(value as RecordEventKind);
              }}
            >
              <SelectTrigger id="awareness-event-kind" className="w-full">
                <SelectValue>
                  {(value: RecordEventKind | null) => (value ? humanize(value) : "Choose the kind of event")}
                </SelectValue>
              </SelectTrigger>
              <SelectContent>
                {RECORD_EVENT_KINDS.map((kind) => (
                  <SelectItem key={kind} value={kind}>
                    {humanize(kind)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          <Field>
            <FieldLabel htmlFor="awareness-date">Occurred on</FieldLabel>
            <div className="grid grid-cols-2 gap-2">
              <Input id="awareness-date" type="date" value={date} onChange={(event) => setDate(event.target.value)} required />
              <Input
                type="time"
                aria-label="Occurred at"
                value={time}
                onChange={(event) => setTime(event.target.value)}
                required
              />
            </div>
            <FieldDescription>Times are in {zone}. The case stores the moment in UTC.</FieldDescription>
          </Field>
          <Field>
            <FieldLabel htmlFor="awareness-basis">Basis</FieldLabel>
            <Textarea
              id="awareness-basis"
              value={basis}
              maxLength={BASIS_MAX_LENGTH}
              onChange={(event) => setBasis(event.target.value)}
              rows={3}
            />
            <FieldDescription>What the organization knew at that point, and where that knowledge came from.</FieldDescription>
          </Field>
        </FieldGroup>
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        <DialogFooter>
          <Button type="submit" disabled={busy}>
            {busy ? "Saving…" : "Add event"}
          </Button>
        </DialogFooter>
      </form>
    </>
  );
}
