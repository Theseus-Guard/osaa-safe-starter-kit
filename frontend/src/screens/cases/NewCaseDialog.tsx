import { FormEvent, useState } from "react";
import { CaseDetail, createCase } from "../../api";
import { describeError } from "../../messages";
import { useSystemTimezone } from "../../system-timezone";
import { offsetLabel, zonedParts, zonedToUtcIso } from "../../time";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";

const TITLE_MAX_LENGTH = 200;

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onCreated: (created: CaseDetail) => void;
}

export function NewCaseDialog({ open, onOpenChange, onCreated }: Props) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        {/* The form is mounted only while the dialog is open, so every opening starts from the current time. */}
        <NewCaseForm
          onCreated={(created) => {
            onOpenChange(false);
            onCreated(created);
          }}
        />
      </DialogContent>
    </Dialog>
  );
}

function NewCaseForm({ onCreated }: { onCreated: (created: CaseDetail) => void }) {
  const { timezone } = useSystemTimezone();
  const zone = timezone ?? "UTC";
  const [title, setTitle] = useState("");
  // The awareness time is entered as a wall-clock date and time in the system timezone, and converted to UTC on submit.
  const [initial] = useState(() => zonedParts(new Date(), zone));
  const [date, setDate] = useState(initial.date);
  const [time, setTime] = useState(initial.time);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const utcIso = zonedToUtcIso(date, time, zone);
  const offset = utcIso === null ? null : offsetLabel(zone, new Date(utcIso));

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    const trimmed = title.trim();
    if (trimmed.length === 0) {
      setError("Enter a title for the case.");
      return;
    }
    if (date === "" || time === "") {
      setError("Enter the date and time you became aware of the incident.");
      return;
    }
    if (utcIso === null) {
      setError(`This date and time does not exist in ${zone} because of a daylight saving change. Choose another time.`);
      return;
    }
    if (new Date(utcIso).getTime() > Date.now()) {
      setError("The awareness time cannot be in the future. Choose the time you became aware, not a later one.");
      return;
    }
    setBusy(true);
    try {
      onCreated(await createCase({ title: trimmed, awareness_time: utcIso }));
    } catch (caught) {
      setError(describeError(caught));
      setBusy(false);
    }
  }

  return (
    <>
      <DialogHeader>
        <DialogTitle>New case</DialogTitle>
        <DialogDescription>Record when you became aware of the incident. Deadlines are calculated from this time.</DialogDescription>
      </DialogHeader>
      <form onSubmit={handleSubmit} className="flex flex-col gap-4">
        <FieldGroup>
          <Field>
            <FieldLabel htmlFor="case-title">Title</FieldLabel>
            <Input
              id="case-title"
              value={title}
              maxLength={TITLE_MAX_LENGTH}
              onChange={(event) => setTitle(event.target.value)}
              required
            />
          </Field>
          <Field>
            <FieldLabel htmlFor="case-date">Awareness date and time</FieldLabel>
            <div className="grid grid-cols-2 gap-2">
              <Input id="case-date" type="date" value={date} onChange={(event) => setDate(event.target.value)} required />
              <Input
                type="time"
                aria-label="Awareness time"
                value={time}
                onChange={(event) => setTime(event.target.value)}
                required
              />
            </div>
            <FieldDescription>
              Times are in {zone}
              {offset !== null && ` (${offset})`}. The case stores the moment in UTC.
            </FieldDescription>
          </Field>
        </FieldGroup>
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        <DialogFooter>
          <Button type="submit" disabled={busy}>
            {busy ? "Creating…" : "Create case"}
          </Button>
        </DialogFooter>
      </form>
    </>
  );
}
