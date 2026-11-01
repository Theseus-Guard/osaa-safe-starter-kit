import { FormEvent, ReactNode, useEffect, useState } from "react";
import { toast } from "sonner";
import {
  ApiError,
  CLASSIFICATION_TEXT_FIELDS,
  Classification,
  ClassificationText,
  ClassificationTextField,
  ClassificationUpdateBody,
  RECORD_EVENT_KINDS,
  RecordEventKind,
  TRIGGERS,
  Trigger,
  getClassification,
  updateClassification,
} from "../../api";
import { describeError } from "../../messages";
import { useSystemTimezone } from "../../system-timezone";
import { zonedParts, zonedToUtcIso } from "../../time";
import { humanize, recordText, triggerLabel } from "./case-format";
import { Timestamp } from "@/components/timestamp";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";

interface Props {
  caseId: string;
  // The case is closed. The classification cannot be changed, so the edit button is not offered.
  closed: boolean;
  // The caller has cases:triage. Without it the classification is shown read-only.
  canWrite: boolean;
  // The caller has cases:read_all. Without it the text is not returned, so the classification cannot be edited safely.
  canReadFullView: boolean;
  // Called after a new version is stored. The caller reloads the history, because the change is audited.
  onRecorded: () => void;
}

const TEXT_FIELD_LABELS: Record<ClassificationTextField, string> = {
  classification_rationale: "Rationale",
  responsible_operator: "Responsible operator",
  affected_versions: "Affected versions",
  approved_scope: "Approved scope",
  scope_permission_source: "Source of the scope permission",
  containment: "Containment",
  evidence_gap_plan: "Evidence gap plan",
};

const TEXT_MAX_LENGTH = 1000;
const NOT_RECORDED = "none";
// The stable code of a stale save. The 409 body also carries current_version, which ApiError exposes.
const CONFLICT_CODE = "classification_version_conflict";

// The trigger value that records that no trigger applies. It cannot be combined with another trigger.
const NONE_APPLIES: Trigger = "none_applies";

export function CaseClassification({ caseId, closed, canWrite, canReadFullView, onRecorded }: Props) {
  const { timezone } = useSystemTimezone();
  const zone = timezone ?? "UTC";
  const [classification, setClassification] = useState<Classification | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  // Bumped when the server reports a version conflict, so the displayed version reloads.
  const [version, setVersion] = useState(0);
  const canEdit = canWrite && !closed && canReadFullView;

  useEffect(() => {
    let stale = false;
    setError(null);
    getClassification(caseId)
      .then((result) => {
        if (!stale) setClassification(result);
      })
      .catch((caught: unknown) => {
        if (!stale) setError(describeError(caught));
      });
    return () => {
      stale = true;
    };
  }, [caseId, version]);

  function applySaved(saved: Classification) {
    setClassification(saved);
    setEditing(false);
    toast.success(`Classification saved as version ${saved.version}.`);
    onRecorded();
  }

  let description = "How the case is classified against the R01 triggers. Each save stores a new version.";
  if (closed) description = "This case is closed, so its classification cannot be changed.";
  else if (!canWrite) description = "The classification of this case. Your account cannot change it.";

  return (
    <Card>
      <CardHeader>
        <CardTitle>Classification</CardTitle>
        <CardDescription>{description}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {classification === null && error === null && (
          <p className="text-sm text-muted-foreground">Loading classification…</p>
        )}
        {classification !== null && <ClassificationView classification={classification} canReadFullView={canReadFullView} />}
        {canEdit && (
          <div>
            <Button variant="outline" onClick={() => setEditing(true)}>
              Edit classification
            </Button>
          </div>
        )}
        {canWrite && !closed && !canReadFullView && (
          <p className="text-sm text-muted-foreground">
            Editing the classification needs the full case view. Without it, the text fields would be cleared on save.
          </p>
        )}
        {canEdit && classification !== null && (
          <Dialog open={editing} onOpenChange={setEditing}>
            <DialogContent className="max-h-[90vh] overflow-y-auto">
              {/* The form is mounted only while the dialog is open, so every opening starts from the current version. */}
              <EditClassificationForm
                caseId={caseId}
                current={classification}
                zone={zone}
                onSaved={applySaved}
                onConflict={() => setVersion((value) => value + 1)}
              />
            </DialogContent>
          </Dialog>
        )}
      </CardContent>
    </Card>
  );
}

function ClassificationView({ classification, canReadFullView }: { classification: Classification; canReadFullView: boolean }) {
  if (classification.version === 0) {
    return <p className="text-sm text-muted-foreground">No classification is recorded for this case yet.</p>;
  }
  return (
    <div className="flex flex-col gap-4 text-sm">
      <p className="text-muted-foreground">
        <span>Version {classification.version}</span>
        {classification.created_at && (
          <>
            {" · saved "}
            <Timestamp value={classification.created_at} />
          </>
        )}
      </p>
      <div className="grid gap-1">
        <span className="font-medium">Triggers</span>
        {classification.triggers.length === 0 ? (
          <span className="text-muted-foreground">No trigger selected.</span>
        ) : (
          <ul className="flex flex-wrap gap-2">
            {classification.triggers.map((trigger) => (
              <li key={trigger}>
                <Badge variant="secondary">{triggerLabel(trigger)}</Badge>
              </li>
            ))}
          </ul>
        )}
      </div>
      <dl className="grid gap-4 sm:grid-cols-2">
        <Detail label="Event">
          {classification.event_kind === null ? "Not recorded" : humanize(classification.event_kind)}
        </Detail>
        <Detail label="Internal incident option">{classification.internal_incident_option ? "Yes" : "No"}</Detail>
        <Detail label="Next update due">
          {classification.next_update_due_at === null ? "Not set" : <Timestamp value={classification.next_update_due_at} />}
        </Detail>
        {CLASSIFICATION_TEXT_FIELDS.map((field) => (
          <Detail key={field} label={TEXT_FIELD_LABELS[field]}>
            <span className="whitespace-pre-wrap">{recordText(classification[field], canReadFullView)}</span>
          </Detail>
        ))}
      </dl>
    </div>
  );
}

function Detail({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="grid gap-1">
      <dt className="font-medium">{label}</dt>
      <dd className="text-muted-foreground">{children}</dd>
    </div>
  );
}

function textsFrom(current: ClassificationText): Record<ClassificationTextField, string> {
  return Object.fromEntries(
    CLASSIFICATION_TEXT_FIELDS.map((field) => [field, current[field] ?? ""]),
  ) as Record<ClassificationTextField, string>;
}

interface FormProps {
  caseId: string;
  current: Classification;
  zone: string;
  onSaved: (saved: Classification) => void;
  onConflict: () => void;
}

function EditClassificationForm({ caseId, current, zone, onSaved, onConflict }: FormProps) {
  // The version the editor opened with. A save against a newer version is refused, so nothing is overwritten silently.
  const [expectedVersion] = useState(current.version);
  const [triggers, setTriggers] = useState<Trigger[]>(current.triggers);
  const [eventKind, setEventKind] = useState<RecordEventKind | null>(current.event_kind);
  const [internalOption, setInternalOption] = useState(current.internal_incident_option);
  const [texts, setTexts] = useState(() => textsFrom(current));
  const [nextParts, setNextParts] = useState(() =>
    current.next_update_due_at === null
      ? { date: "", time: "" }
      : zonedParts(new Date(current.next_update_due_at), zone),
  );
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function toggleTrigger(trigger: Trigger, checked: boolean) {
    setTriggers((list) => {
      const without = list.filter((item) => item !== trigger);
      if (!checked) return without;
      // "None applies" stands alone: choosing it clears the other triggers, and choosing another trigger clears it.
      if (trigger === NONE_APPLIES) return [trigger];
      return [...without.filter((item) => item !== NONE_APPLIES), trigger];
    });
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    const cleanTexts = Object.fromEntries(
      CLASSIFICATION_TEXT_FIELDS.map((field) => {
        const trimmed = texts[field].trim();
        return [field, trimmed === "" ? null : trimmed];
      }),
    ) as ClassificationText;
    // The backend requires a rationale when the internal incident option is chosen.
    if (internalOption && cleanTexts.classification_rationale === null) {
      setError("Enter the rationale. It is required when the internal incident option is chosen.");
      return;
    }
    let nextUpdateDueAt: string | null = null;
    if (nextParts.date !== "" || nextParts.time !== "") {
      if (nextParts.date === "" || nextParts.time === "") {
        setError("Enter both the date and the time of the next update, or neither.");
        return;
      }
      nextUpdateDueAt = zonedToUtcIso(nextParts.date, nextParts.time, zone);
      if (nextUpdateDueAt === null) {
        setError(`This date and time does not exist in ${zone} because of a daylight saving change. Choose another time.`);
        return;
      }
    }
    // Every field is sent. The backend clears a field that is not sent.
    const body: ClassificationUpdateBody = {
      ...cleanTexts,
      expected_version: expectedVersion,
      triggers,
      event_kind: eventKind,
      internal_incident_option: internalOption,
      next_update_due_at: nextUpdateDueAt,
    };
    setBusy(true);
    try {
      onSaved(await updateClassification(caseId, body));
    } catch (caught) {
      setBusy(false);
      if (caught instanceof ApiError && caught.code === CONFLICT_CODE) {
        const version = caught.currentVersion;
        setError(
          version === null
            ? describeError(caught)
            : `${describeError(caught)} The current version is ${version}.`,
        );
        onConflict();
      } else {
        setError(describeError(caught));
      }
    }
  }

  return (
    <>
      <DialogHeader>
        <DialogTitle>Edit classification</DialogTitle>
        <DialogDescription>
          Saving stores a new version. Earlier versions stay on record. Editing started from version {expectedVersion}
          {expectedVersion === 0 ? " (none recorded yet)" : ""}.
        </DialogDescription>
      </DialogHeader>
      <form onSubmit={handleSubmit} className="flex flex-col gap-4">
        <fieldset className="grid gap-2">
          <legend className="mb-1 text-sm font-medium">Triggers</legend>
          {TRIGGERS.map((trigger) => (
            <label key={trigger} className="flex items-center gap-2 text-sm">
              <Checkbox
                checked={triggers.includes(trigger)}
                onCheckedChange={(checked) => toggleTrigger(trigger, checked)}
              />
              {triggerLabel(trigger)}
            </label>
          ))}
        </fieldset>
        <FieldGroup>
          <Field>
            <FieldLabel htmlFor="classification-event-kind">Event</FieldLabel>
            <Select
              value={eventKind ?? NOT_RECORDED}
              onValueChange={(value) => {
                if (value) setEventKind(value === NOT_RECORDED ? null : (value as RecordEventKind));
              }}
            >
              <SelectTrigger id="classification-event-kind" className="w-full">
                <SelectValue>
                  {(value: string | null) =>
                    value === null || value === NOT_RECORDED ? "Not recorded" : humanize(value)
                  }
                </SelectValue>
              </SelectTrigger>
              <SelectContent>
                <SelectItem value={NOT_RECORDED}>Not recorded</SelectItem>
                {RECORD_EVENT_KINDS.map((kind) => (
                  <SelectItem key={kind} value={kind}>
                    {humanize(kind)}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </Field>
          <label className="flex items-center gap-2 text-sm">
            <Checkbox checked={internalOption} onCheckedChange={(checked) => setInternalOption(checked)} />
            Internal incident option
          </label>
          {CLASSIFICATION_TEXT_FIELDS.map((field) => (
            <Field key={field}>
              <FieldLabel htmlFor={`classification-${field}`}>{TEXT_FIELD_LABELS[field]}</FieldLabel>
              <Textarea
                id={`classification-${field}`}
                value={texts[field]}
                maxLength={TEXT_MAX_LENGTH}
                onChange={(event) => setTexts((previous) => ({ ...previous, [field]: event.target.value }))}
                rows={2}
              />
              {field === "classification_rationale" && (
                <FieldDescription>Required when the internal incident option is chosen.</FieldDescription>
              )}
            </Field>
          ))}
          <Field>
            <FieldLabel htmlFor="classification-next-date">Next update due</FieldLabel>
            <div className="grid grid-cols-2 gap-2">
              <Input
                id="classification-next-date"
                type="date"
                value={nextParts.date}
                onChange={(event) => setNextParts((parts) => ({ ...parts, date: event.target.value }))}
              />
              <Input
                type="time"
                aria-label="Next update time"
                value={nextParts.time}
                onChange={(event) => setNextParts((parts) => ({ ...parts, time: event.target.value }))}
              />
            </div>
            <FieldDescription>Optional. Times are in {zone}. Clear both fields to remove it.</FieldDescription>
          </Field>
        </FieldGroup>
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        <DialogFooter>
          <Button type="submit" disabled={busy}>
            {busy ? "Saving…" : "Save new version"}
          </Button>
        </DialogFooter>
      </form>
    </>
  );
}
