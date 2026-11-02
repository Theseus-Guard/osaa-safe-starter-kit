import { FormEvent, useEffect, useState } from "react";
import { toast } from "sonner";
import {
  CATALOG_RESULTS,
  CatalogEntry,
  CatalogResult,
  Recommendation,
  addCatalogVersion,
  correctCatalogEntry,
  disputeCatalogEntry,
  listCatalogEntries,
  listRecommendations,
  supersedeCatalogEntry,
  verifyCatalogEntry,
  withdrawCatalogEntry,
} from "../../api";
import { describeError } from "../../messages";
import {
  CATALOG_BODY_LIMIT,
  CATALOG_FIELD_LIMIT,
  CATALOG_REFERENCE_LIMIT,
  CATALOG_TITLE_LIMIT,
  checkText,
} from "./CatalogAddForms";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";

// Native selects keep the choice keyboard-friendly and easy to read in tests.
const SELECT_CLASS = "h-9 w-full rounded-md border border-input bg-background px-3 text-sm";

// Shared by every entry form: the error line, and a flag while the request is running.
function useFormState() {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  return { error, setError, busy, setBusy };
}

function ErrorLine({ message }: { message: string | null }) {
  if (message === null) return null;
  return (
    <Alert variant="destructive">
      <AlertDescription>{message}</AlertDescription>
    </Alert>
  );
}

interface ContentFormProps {
  entry: CatalogEntry;
  // version: a new version with the same or new content. correction: the same, with a reason.
  mode: "version" | "correction";
  onSaved: () => void;
}

// Needs catalog:write and an open entry. The expected version is the one shown on the page, so a stale form is refused.
export function CatalogContentForm({ entry, mode, onSaved }: ContentFormProps) {
  const [title, setTitle] = useState(entry.current.title);
  const [body, setBody] = useState(entry.current.body);
  const [reason, setReason] = useState("");
  const [reference, setReference] = useState("");
  const { error, setError, busy, setBusy } = useFormState();
  const correction = mode === "correction";

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const problem =
      checkText(title, "a title", CATALOG_TITLE_LIMIT) ??
      checkText(body, "a body", CATALOG_BODY_LIMIT) ??
      (correction ? checkText(reason, "a reason", CATALOG_FIELD_LIMIT) : null) ??
      checkText(reference, "a restricted reference", CATALOG_REFERENCE_LIMIT, false);
    if (problem !== null) {
      setError(problem);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const content = {
        title: title.trim(),
        body: body.trim(),
        restricted_reference: reference.trim() === "" ? null : reference.trim(),
      };
      if (correction) {
        await correctCatalogEntry(entry.id, entry.version, { ...content, reason: reason.trim() });
        toast.success("Correction saved. The earlier version is kept.");
      } else {
        await addCatalogVersion(entry.id, entry.version, content);
        toast.success("Version saved. The earlier version is kept.");
      }
      onSaved();
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>{correction ? "Correct the entry" : "Save a new version"}</CardTitle>
        <CardDescription>
          {correction
            ? "A correction writes the new content and the reason. The earlier version stays in the history."
            : "A new version keeps the earlier one. It does not inherit the verification of the earlier content."}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form
          className="flex flex-col gap-3"
          aria-label={correction ? "Correct the entry" : "Save a new version"}
          onSubmit={(event) => void handleSubmit(event)}
        >
          <Field>
            <FieldLabel htmlFor={`${mode}-title`}>Title</FieldLabel>
            <Input id={`${mode}-title`} value={title} onChange={(event) => setTitle(event.target.value)} />
          </Field>
          <Field>
            <FieldLabel htmlFor={`${mode}-body`}>Body</FieldLabel>
            <Textarea id={`${mode}-body`} rows={4} value={body} onChange={(event) => setBody(event.target.value)} />
          </Field>
          {correction && (
            <Field>
              <FieldLabel htmlFor={`${mode}-reason`}>Reason for the correction</FieldLabel>
              <Input id={`${mode}-reason`} value={reason} onChange={(event) => setReason(event.target.value)} />
            </Field>
          )}
          <Field>
            <FieldLabel htmlFor={`${mode}-reference`}>Restricted reference (optional, leave blank to keep it)</FieldLabel>
            <Input id={`${mode}-reference`} value={reference} autoComplete="off" onChange={(event) => setReference(event.target.value)} />
          </Field>
          <ErrorLine message={error} />
          <div>
            <Button type="submit" disabled={busy}>
              {correction ? "Save correction" : "Save version"}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

interface ReasonFormProps {
  entry: CatalogEntry;
  action: "dispute" | "withdraw";
  onSaved: () => void;
}

// Needs catalog:write and an open entry. Both keep the content; a withdrawal closes the entry.
export function CatalogReasonForm({ entry, action, onSaved }: ReasonFormProps) {
  const [reason, setReason] = useState("");
  const { error, setError, busy, setBusy } = useFormState();
  const withdraw = action === "withdraw";

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const problem = checkText(reason, "a reason", CATALOG_FIELD_LIMIT);
    if (problem !== null) {
      setError(problem);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      if (withdraw) {
        await withdrawCatalogEntry(entry.id, entry.version, reason.trim());
        toast.success("Entry withdrawn. Its history is kept.");
      } else {
        await disputeCatalogEntry(entry.id, entry.version, reason.trim());
        toast.success("Dispute recorded. The entry content is unchanged.");
      }
      onSaved();
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>{withdraw ? "Withdraw the entry" : "Dispute the entry"}</CardTitle>
        <CardDescription>
          {withdraw
            ? "A withdrawn entry accepts no further change. Its history stays visible."
            : "A dispute records a challenge to the entry. The content does not change."}
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form
          className="flex flex-col gap-3"
          aria-label={withdraw ? "Withdraw the entry" : "Dispute the entry"}
          onSubmit={(event) => void handleSubmit(event)}
        >
          <Field>
            <FieldLabel htmlFor={`${action}-reason`}>Reason</FieldLabel>
            <Input id={`${action}-reason`} value={reason} onChange={(event) => setReason(event.target.value)} />
          </Field>
          <ErrorLine message={error} />
          <div>
            <Button type="submit" variant={withdraw ? "destructive" : "default"} disabled={busy}>
              {withdraw ? "Withdraw entry" : "Record dispute"}
            </Button>
          </div>
        </form>
      </CardContent>
    </Card>
  );
}

interface SupersedeFormProps {
  entry: CatalogEntry;
  onSaved: () => void;
}

// Needs catalog:write. The replacement must be another open entry. The old entry is kept and closed.
export function CatalogSupersedeForm({ entry, onSaved }: SupersedeFormProps) {
  const [options, setOptions] = useState<CatalogEntry[] | null>(null);
  const [replacementId, setReplacementId] = useState("");
  const [reason, setReason] = useState("");
  const { error, setError, busy, setBusy } = useFormState();

  useEffect(() => {
    let stale = false;
    listCatalogEntries()
      .then((page) => {
        if (stale) return;
        // Only open entries can replace another entry, and an entry cannot replace itself.
        const open = page.items.filter(
          (item) => item.id !== entry.id && (item.status === "active" || item.status === "disputed"),
        );
        setOptions(open);
        setReplacementId(open[0]?.id ?? "");
      })
      .catch((caught: unknown) => {
        if (!stale) setError(describeError(caught));
      });
    return () => {
      stale = true;
    };
  }, [entry.id, setError]);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (replacementId === "") {
      setError("Choose the open entry that replaces this one.");
      return;
    }
    const problem = checkText(reason, "a reason", CATALOG_FIELD_LIMIT, false);
    if (problem !== null) {
      setError(problem);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await supersedeCatalogEntry(entry.id, entry.version, replacementId, reason.trim() === "" ? null : reason.trim());
      toast.success("Entry superseded. Its history is kept.");
      onSaved();
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Supersede the entry</CardTitle>
        <CardDescription>Replace this entry with another open entry. The old entry is closed and kept.</CardDescription>
      </CardHeader>
      <CardContent>
        {options !== null && options.length === 0 ? (
          <p className="text-sm text-muted-foreground">No other open entry is available to replace this one.</p>
        ) : (
          <form className="flex flex-col gap-3" aria-label="Supersede the entry" onSubmit={(event) => void handleSubmit(event)}>
            <Field>
              <FieldLabel htmlFor={`supersede-${entry.id}`}>Replacement entry</FieldLabel>
              <select
                id={`supersede-${entry.id}`}
                className={SELECT_CLASS}
                value={replacementId}
                disabled={options === null}
                onChange={(event) => setReplacementId(event.target.value)}
              >
                {(options ?? []).map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.current.title}
                  </option>
                ))}
              </select>
            </Field>
            <Field>
              <FieldLabel htmlFor={`supersede-reason-${entry.id}`}>Reason (optional)</FieldLabel>
              <Input
                id={`supersede-reason-${entry.id}`}
                value={reason}
                onChange={(event) => setReason(event.target.value)}
              />
            </Field>
            <ErrorLine message={error} />
            <div>
              <Button type="submit" disabled={busy || options === null || options.length === 0}>
                Supersede entry
              </Button>
            </div>
          </form>
        )}
      </CardContent>
    </Card>
  );
}

interface VerifyFormProps {
  entry: CatalogEntry;
  onSaved: () => void;
}

const VERIFY_TEXT_FIELDS = [
  ["subject", "Subject"],
  ["scope", "Scope"],
  ["assumptions", "Assumptions"],
  ["evidencePath", "Evidence path"],
  ["failureBehavior", "Failure behaviour"],
  ["errorClass", "Error class"],
  ["uncertainty", "Uncertainty"],
  ["method", "Method"],
] as const;

type VerifyField = (typeof VERIFY_TEXT_FIELDS)[number][0];

// Needs catalog:write and an open entry. Records a verification run of a recommendation and attaches it to this entry.
export function CatalogVerifyForm({ entry, onSaved }: VerifyFormProps) {
  const [recommendations, setRecommendations] = useState<Recommendation[] | null>(null);
  const [recommendationId, setRecommendationId] = useState("");
  const [result, setResult] = useState<CatalogResult>("pass");
  const [fields, setFields] = useState<Record<VerifyField, string>>({
    subject: "",
    scope: "",
    assumptions: "",
    evidencePath: "",
    failureBehavior: "",
    errorClass: "",
    uncertainty: "",
    method: "",
  });
  const { error, setError, busy, setBusy } = useFormState();

  useEffect(() => {
    let stale = false;
    listRecommendations()
      .then((page) => {
        if (stale) return;
        setRecommendations(page.items);
        setRecommendationId(page.items[0]?.id ?? "");
      })
      .catch((caught: unknown) => {
        if (!stale) setError(describeError(caught));
      });
    return () => {
      stale = true;
    };
  }, [setError]);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (recommendationId === "") {
      setError("Choose the recommendation this run checks.");
      return;
    }
    for (const [key, label] of VERIFY_TEXT_FIELDS) {
      const problem = checkText(fields[key], label.toLowerCase(), CATALOG_FIELD_LIMIT);
      if (problem !== null) {
        setError(problem);
        return;
      }
    }
    setBusy(true);
    setError(null);
    try {
      const updated = await verifyCatalogEntry(entry, recommendationId, {
        subject: fields.subject.trim(),
        scope: fields.scope.trim(),
        assumptions: fields.assumptions.trim(),
        evidence_path: fields.evidencePath.trim(),
        failure_behavior: fields.failureBehavior.trim(),
        error_class: fields.errorClass.trim(),
        uncertainty: fields.uncertainty.trim(),
        method: fields.method.trim(),
        result,
      });
      if (updated.current.verified) {
        toast.success("Verification recorded. The entry is verified.");
      } else {
        toast.success("Verification recorded. The entry is not verified: only a passing run verifies it.");
      }
      onSaved();
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Record a verification run</CardTitle>
        <CardDescription>
          The run is its own record. It is attached to this entry as a new version with the same content. Only a pass
          verifies the entry.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {recommendations !== null && recommendations.length === 0 ? (
          <p className="text-sm text-muted-foreground">Record a recommendation first. A run checks a recommendation.</p>
        ) : (
          <form className="flex flex-col gap-3" aria-label="Record a verification run" onSubmit={(event) => void handleSubmit(event)}>
            <Field>
              <FieldLabel htmlFor={`verify-recommendation-${entry.id}`}>Recommendation</FieldLabel>
              <select
                id={`verify-recommendation-${entry.id}`}
                className={SELECT_CLASS}
                value={recommendationId}
                disabled={recommendations === null}
                onChange={(event) => setRecommendationId(event.target.value)}
              >
                {(recommendations ?? []).map((item) => (
                  <option key={item.id} value={item.id}>
                    {item.failure_or_system}
                  </option>
                ))}
              </select>
            </Field>
            <Field>
              <FieldLabel htmlFor={`verify-result-${entry.id}`}>Result</FieldLabel>
              <select
                id={`verify-result-${entry.id}`}
                className={SELECT_CLASS}
                value={result}
                onChange={(event) => setResult(event.target.value as CatalogResult)}
              >
                {CATALOG_RESULTS.map((value) => (
                  <option key={value} value={value}>
                    {value}
                  </option>
                ))}
              </select>
            </Field>
            {VERIFY_TEXT_FIELDS.map(([key, label]) => (
              <Field key={key}>
                <FieldLabel htmlFor={`verify-${key}-${entry.id}`}>{label}</FieldLabel>
                <Input
                  id={`verify-${key}-${entry.id}`}
                  value={fields[key]}
                  onChange={(event) => setFields((current) => ({ ...current, [key]: event.target.value }))}
                />
              </Field>
            ))}
            <ErrorLine message={error} />
            <div>
              <Button type="submit" disabled={busy || recommendations === null}>
                Record verification
              </Button>
            </div>
          </form>
        )}
      </CardContent>
    </Card>
  );
}
