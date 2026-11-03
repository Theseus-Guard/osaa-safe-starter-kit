import { FormEvent, useEffect, useState } from "react";
import { toast } from "sonner";
import { DUTY_KINDS, DutyKind, DutyStatus, LegalDuty, addLegalDuty, listLegalDuties, updateLegalDuty } from "../../api";
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
import { Field, FieldDescription, FieldGroup, FieldLabel, FieldTitle } from "@/components/ui/field";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";

interface Props {
  caseId: string;
  // The case is closed. Duties cannot be added or changed.
  closed: boolean;
  // The caller has cases:triage. Without it the duties are listed read-only.
  canWrite: boolean;
  // The caller has cases:read_all. Without it the duty text is not returned.
  canReadFullView: boolean;
  // Called after a duty is stored or changed. The caller reloads the history, because the change is audited.
  onRecorded: () => void;
}

const DESCRIPTION_MAX_LENGTH = 1000;
const SOURCE_MAX_LENGTH = 500;
const JURISDICTION_MAX_LENGTH = 200;

export function CaseLegalDuties({ caseId, closed, canWrite, canReadFullView, onRecorded }: Props) {
  const [items, setItems] = useState<LegalDuty[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  const [busyDutyId, setBusyDutyId] = useState<string | null>(null);
  // Bumped after a duty is added so the list reloads from the server.
  const [version, setVersion] = useState(0);
  const canChange = canWrite && !closed;

  useEffect(() => {
    let stale = false;
    setError(null);
    listLegalDuties(caseId)
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

  async function changeStatus(duty: LegalDuty, status: DutyStatus) {
    setBusyDutyId(duty.id);
    try {
      const updated = await updateLegalDuty(caseId, duty.id, { status }, duty.revision);
      setItems((current) => current?.map((row) => (row.id === updated.id ? updated : row)) ?? null);
      toast.success(`${humanize(duty.kind)} duty is now ${humanize(status).toLowerCase()}.`);
      onRecorded();
    } catch (caught) {
      toast.error(describeError(caught));
    } finally {
      setBusyDutyId(null);
    }
  }

  let description = "Legal duties keep their own due times. They do not change the SAFE deadline tasks of the case.";
  if (closed) description = "This case is closed, so its legal duties cannot be changed.";
  else if (!canWrite) description = "Legal duties of this case. Your account cannot change them.";

  return (
    <Card>
      <CardHeader>
        <CardTitle>Legal duties</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {items === null && error === null && <p className="text-sm text-muted-foreground">Loading legal duties…</p>}
        {items !== null && items.length === 0 && (
          <p className="text-sm text-muted-foreground">No legal duty is recorded for this case.</p>
        )}
        {items !== null && items.length > 0 && (
          <div className="overflow-hidden rounded-md border">
            <Table>
              <TableHeader className="bg-muted">
                <TableRow>
                  <TableHead>Kind</TableHead>
                  <TableHead>Duty</TableHead>
                  <TableHead>Due</TableHead>
                  <TableHead>Status</TableHead>
                  {canChange && <TableHead>Actions</TableHead>}
                </TableRow>
              </TableHeader>
              <TableBody>
                {items.map((duty) => (
                  <TableRow key={duty.id}>
                    <TableCell>
                      <Badge variant="secondary">{humanize(duty.kind)}</Badge>
                    </TableCell>
                    <TableCell className="whitespace-normal">
                      <p>{recordText(duty.description, canReadFullView)}</p>
                      <p className="text-xs text-muted-foreground">
                        Source: {recordText(duty.source_reference, canReadFullView)}
                      </p>
                      {duty.jurisdiction !== null && (
                        <p className="text-xs text-muted-foreground">Jurisdiction: {duty.jurisdiction}</p>
                      )}
                    </TableCell>
                    <TableCell>
                      <Timestamp value={duty.due_at} />
                    </TableCell>
                    <TableCell>
                      <Badge variant="outline">{humanize(duty.status)}</Badge>
                    </TableCell>
                    {canChange && (
                      <TableCell>
                        <div className="flex flex-wrap gap-2">
                          {duty.status === "open" ? (
                            <Button
                              size="sm"
                              variant="outline"
                              disabled={busyDutyId === duty.id}
                              aria-label={`Mark done: ${humanize(duty.kind)} duty`}
                              onClick={() => void changeStatus(duty, "done")}
                            >
                              Mark done
                            </Button>
                          ) : (
                            <Button
                              size="sm"
                              variant="outline"
                              disabled={busyDutyId === duty.id}
                              aria-label={`Set open: ${humanize(duty.kind)} duty`}
                              onClick={() => void changeStatus(duty, "open")}
                            >
                              Set open
                            </Button>
                          )}
                        </div>
                      </TableCell>
                    )}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
        {canChange && (
          <div>
            <Button variant="outline" onClick={() => setOpen(true)}>
              Add legal duty
            </Button>
          </div>
        )}
        {canChange && (
          <AddLegalDutyDialog
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

function AddLegalDutyDialog({ caseId, open, onOpenChange, onRecorded }: DialogProps) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        {/* The form is mounted only while the dialog is open, so every opening starts empty. */}
        <AddLegalDutyForm
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

function AddLegalDutyForm({ caseId, onRecorded }: { caseId: string; onRecorded: () => void }) {
  const { timezone } = useSystemTimezone();
  const zone = timezone ?? "UTC";
  const [kind, setKind] = useState<DutyKind | null>(null);
  const [description, setDescription] = useState("");
  const [sourceReference, setSourceReference] = useState("");
  const [jurisdiction, setJurisdiction] = useState("");
  // The due time is entered as a wall-clock date and time in the system timezone, and converted to UTC on submit.
  const [initial] = useState(() => zonedParts(new Date(), zone));
  const [date, setDate] = useState(initial.date);
  const [time, setTime] = useState(initial.time);
  const [error, setError] = useState<string | null>(null);
  // The field the current error is about, so that the field can point at the message.
  const [errorField, setErrorField] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setErrorField(null);
    if (kind === null) {
      setErrorField("duty-kind");
      setError("Choose whether the duty is statutory or contractual.");
      return;
    }
    const trimmedDescription = description.trim();
    const trimmedSource = sourceReference.trim();
    if (trimmedDescription.length === 0 || trimmedSource.length === 0) {
      setErrorField(trimmedDescription.length === 0 ? "duty-description" : "duty-source");
      setError("Enter the duty and the statute or clause that creates it.");
      return;
    }
    if (date === "" || time === "") {
      setErrorField("duty-when");
      setError("Enter the date and time the duty is due.");
      return;
    }
    const dueAt = zonedToUtcIso(date, time, zone);
    if (dueAt === null) {
      setErrorField("duty-when");
      setError(`This date and time does not exist in ${zone} because of a daylight saving change. Choose another time.`);
      return;
    }
    const trimmedJurisdiction = jurisdiction.trim();
    setBusy(true);
    try {
      await addLegalDuty(caseId, {
        kind,
        description: trimmedDescription,
        source_reference: trimmedSource,
        jurisdiction: trimmedJurisdiction === "" ? null : trimmedJurisdiction,
        due_at: dueAt,
      });
      toast.success("Legal duty recorded.");
      onRecorded();
    } catch (caught) {
      setError(describeError(caught));
      setBusy(false);
    }
  }

  return (
    <>
      <DialogHeader>
        <DialogTitle>Add legal duty</DialogTitle>
        <DialogDescription>A legal duty has its own due time and does not change the SAFE deadline tasks.</DialogDescription>
      </DialogHeader>
      <form onSubmit={handleSubmit} className="flex flex-col gap-4">
        <FieldGroup>
          <Field>
            <FieldLabel htmlFor="duty-kind">
              Kind <span className="font-normal text-muted-foreground">(required)</span>
            </FieldLabel>
            <Select
              value={kind}
              onValueChange={(value) => {
                if (value) setKind(value as DutyKind);
              }}
            >
              <SelectTrigger
                id="duty-kind"
                className="w-full"
                aria-invalid={errorField === "duty-kind" || undefined}
                aria-describedby={errorField === "duty-kind" ? "duty-error" : undefined}
              >
                <SelectValue>{(value: DutyKind | null) => (value ? humanize(value) : "Choose a kind")}</SelectValue>
              </SelectTrigger>
              <SelectContent>
                {DUTY_KINDS.map((item) => (
                  <SelectItem key={item} value={item}>
                    {humanize(item)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          <Field>
            <FieldLabel htmlFor="duty-description">
              Duty <span className="font-normal text-muted-foreground">(required)</span>
            </FieldLabel>
            <Textarea
              id="duty-description"
              aria-invalid={errorField === "duty-description" || undefined}
              aria-describedby={errorField === "duty-description" ? "duty-error" : undefined}
              value={description}
              maxLength={DESCRIPTION_MAX_LENGTH}
              onChange={(event) => setDescription(event.target.value)}
              rows={3}
            />
          </Field>
          <Field>
            <FieldLabel htmlFor="duty-source">
              Source <span className="font-normal text-muted-foreground">(required)</span>
            </FieldLabel>
            <Input
              id="duty-source"
              aria-invalid={errorField === "duty-source" || undefined}
              aria-describedby={errorField === "duty-source" ? "duty-error" : undefined}
              value={sourceReference}
              maxLength={SOURCE_MAX_LENGTH}
              onChange={(event) => setSourceReference(event.target.value)}
            />
            <FieldDescription>The statute or the contract clause that creates the duty.</FieldDescription>
          </Field>
          <Field>
            <FieldLabel htmlFor="duty-jurisdiction">Jurisdiction</FieldLabel>
            <Input
              id="duty-jurisdiction"
              value={jurisdiction}
              maxLength={JURISDICTION_MAX_LENGTH}
              onChange={(event) => setJurisdiction(event.target.value)}
            />
            <FieldDescription>Optional.</FieldDescription>
          </Field>
          <Field>
            <FieldTitle id="duty-when-label">
              Due on <span className="font-normal text-muted-foreground">(required)</span>
            </FieldTitle>
            <div role="group" aria-labelledby="duty-when-label" className="grid grid-cols-2 gap-2">
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="duty-date">Date</Label>
                <Input
                  id="duty-date"
                  type="date"
                  value={date}
                  onChange={(event) => setDate(event.target.value)}
                  aria-invalid={errorField === "duty-when" || undefined}
                  aria-describedby={errorField === "duty-when" ? "duty-error" : undefined}
                  required
                />
              </div>
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="duty-time">Time</Label>
                <Input
                  id="duty-time"
                  type="time"
                  value={time}
                  onChange={(event) => setTime(event.target.value)}
                  aria-invalid={errorField === "duty-when" || undefined}
                  aria-describedby={errorField === "duty-when" ? "duty-error" : undefined}
                  required
                />
              </div>
            </div>
            <FieldDescription>Times are in {zone}. The case stores the moment in UTC.</FieldDescription>
          </Field>
        </FieldGroup>
        {error && (
          <Alert variant="destructive" id="duty-error">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        <DialogFooter>
          <Button type="submit" disabled={busy}>
            {busy ? "Saving…" : "Add duty"}
          </Button>
        </DialogFooter>
      </form>
    </>
  );
}
