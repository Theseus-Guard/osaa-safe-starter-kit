import { FormEvent, useState } from "react";
import { toast } from "sonner";
import { CatalogEntry, Recommendation, createCatalogEntry, createRecommendation } from "../../api";
import { describeError } from "../../messages";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

// The same limits as the API (safe_kit.catalog). The server checks them again.
export const CATALOG_TITLE_LIMIT = 200;
export const CATALOG_BODY_LIMIT = 4000;
export const CATALOG_FIELD_LIMIT = 1000;
export const CATALOG_REFERENCE_LIMIT = 2000;

// Returns a message when the text is empty or longer than the limit, and null when it is valid.
export function checkText(value: string, label: string, limit: number, required = true): string | null {
  const text = value.trim();
  if (text === "") return required ? `Enter ${label}.` : null;
  if (text.length > limit) return `${label} must be at most ${limit} characters.`;
  return null;
}

interface EntryFormProps {
  onCreated: (entry: CatalogEntry) => void;
}

// Needs catalog:write. Creates the first version of a new entry (version 1).
export function CatalogEntryForm({ onCreated }: EntryFormProps) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [reference, setReference] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const problem =
      checkText(title, "a title", CATALOG_TITLE_LIMIT) ??
      checkText(body, "a body", CATALOG_BODY_LIMIT) ??
      checkText(reference, "a restricted reference", CATALOG_REFERENCE_LIMIT, false);
    if (problem !== null) {
      setError(problem);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const created = await createCatalogEntry({
        title: title.trim(),
        body: body.trim(),
        restricted_reference: reference.trim() === "" ? null : reference.trim(),
      });
      toast.success("Catalog entry created.");
      setTitle("");
      setBody("");
      setReference("");
      onCreated(created);
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>New catalog entry</CardTitle>
        <CardDescription>
          A new entry starts at version 1 and is not verified. Verify it later with a verification run.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form className="flex flex-col gap-3" aria-label="New catalog entry" onSubmit={(event) => void handleSubmit(event)}>
          <Field>
            <FieldLabel htmlFor="entry-title">Title</FieldLabel>
            <Input id="entry-title" value={title} maxLength={CATALOG_TITLE_LIMIT} onChange={(event) => setTitle(event.target.value)} />
          </Field>
          <Field>
            <FieldLabel htmlFor="entry-body">Body</FieldLabel>
            <Textarea id="entry-body" rows={4} value={body} onChange={(event) => setBody(event.target.value)} />
          </Field>
          <Field>
            <FieldLabel htmlFor="entry-reference">Restricted reference (optional)</FieldLabel>
            <Input
              id="entry-reference"
              value={reference}
              autoComplete="off"
              onChange={(event) => setReference(event.target.value)}
            />
          </Field>
          {error && (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
          <div>
            <Button type="submit" disabled={busy}>
              Create entry
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

interface RecommendationFormProps {
  onCreated: (recommendation: Recommendation) => void;
}

// Needs catalog:write. A recommendation is a suggestion: it carries no result and is not verified by being recorded.
export function RecommendationForm({ onCreated }: RecommendationFormProps) {
  const [values, setValues] = useState({
    failureOrSystem: "",
    requiredDefenceOutcome: "",
    minimumControl: "",
    controlAlternative: "",
    responsible: "",
    evidenceToKeep: "",
    dueAt: "",
  });
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function set(field: keyof typeof values, value: string) {
    setValues((current) => ({ ...current, [field]: value }));
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const problem =
      checkText(values.failureOrSystem, "the failure or system", CATALOG_FIELD_LIMIT) ??
      checkText(values.requiredDefenceOutcome, "the required defence outcome", CATALOG_FIELD_LIMIT) ??
      checkText(values.minimumControl, "the minimum control", CATALOG_FIELD_LIMIT) ??
      checkText(values.controlAlternative, "an alternative control", CATALOG_FIELD_LIMIT, false) ??
      checkText(values.responsible, "who is responsible", CATALOG_FIELD_LIMIT) ??
      checkText(values.evidenceToKeep, "the evidence to keep", CATALOG_FIELD_LIMIT) ??
      (values.dueAt === "" ? "Enter the due date and time." : null);
    if (problem !== null) {
      setError(problem);
      return;
    }
    const due = new Date(values.dueAt);
    if (Number.isNaN(due.getTime())) {
      setError("Enter a valid due date and time.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const created = await createRecommendation({
        failure_or_system: values.failureOrSystem.trim(),
        required_defence_outcome: values.requiredDefenceOutcome.trim(),
        minimum_control: values.minimumControl.trim(),
        control_alternative: values.controlAlternative.trim() === "" ? null : values.controlAlternative.trim(),
        responsible: values.responsible.trim(),
        evidence_to_keep: values.evidenceToKeep.trim(),
        // The form reads a local date and time; the API takes an instant with its time zone.
        due_at: due.toISOString(),
      });
      toast.success("Recommendation saved. It is a suggestion until a passing verification run is recorded.");
      setValues({
        failureOrSystem: "",
        requiredDefenceOutcome: "",
        minimumControl: "",
        controlAlternative: "",
        responsible: "",
        evidenceToKeep: "",
        dueAt: "",
      });
      onCreated(created);
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>New recommendation</CardTitle>
        <CardDescription>
          A suggested defence for a failure or system. Recording it changes no entry; a verification run checks it.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form className="flex flex-col gap-3" aria-label="New recommendation" onSubmit={(event) => void handleSubmit(event)}>
          <Field>
            <FieldLabel htmlFor="rec-failure">Failure or system</FieldLabel>
            <Input id="rec-failure" value={values.failureOrSystem} onChange={(event) => set("failureOrSystem", event.target.value)} />
          </Field>
          <Field>
            <FieldLabel htmlFor="rec-outcome">Required defence outcome</FieldLabel>
            <Input
              id="rec-outcome"
              value={values.requiredDefenceOutcome}
              onChange={(event) => set("requiredDefenceOutcome", event.target.value)}
            />
          </Field>
          <Field>
            <FieldLabel htmlFor="rec-control">Minimum control</FieldLabel>
            <Input id="rec-control" value={values.minimumControl} onChange={(event) => set("minimumControl", event.target.value)} />
          </Field>
          <Field>
            <FieldLabel htmlFor="rec-alternative">Alternative control (optional)</FieldLabel>
            <Input
              id="rec-alternative"
              value={values.controlAlternative}
              onChange={(event) => set("controlAlternative", event.target.value)}
            />
          </Field>
          <Field>
            <FieldLabel htmlFor="rec-responsible">Responsible</FieldLabel>
            <Input id="rec-responsible" value={values.responsible} onChange={(event) => set("responsible", event.target.value)} />
          </Field>
          <Field>
            <FieldLabel htmlFor="rec-evidence">Evidence to keep</FieldLabel>
            <Input id="rec-evidence" value={values.evidenceToKeep} onChange={(event) => set("evidenceToKeep", event.target.value)} />
          </Field>
          <Field>
            <FieldLabel htmlFor="rec-due">Due (your local time)</FieldLabel>
            <Input id="rec-due" type="datetime-local" value={values.dueAt} onChange={(event) => set("dueAt", event.target.value)} />
          </Field>
          {error && (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
          <div>
            <Button type="submit" disabled={busy}>
              Save recommendation
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}
