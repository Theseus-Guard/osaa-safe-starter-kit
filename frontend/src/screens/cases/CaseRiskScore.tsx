import { FormEvent, useEffect, useState } from "react";
import { toast } from "sonner";
import {
  ApiError,
  ImpactScores,
  ImpactValue,
  ImpactScoresUpdateBody,
  RiskFields,
  getImpactScores,
  updateImpactScores,
} from "../../api";
import { describeError } from "../../messages";
import { IMPACT_VALUE_LABELS, ImpactKey, formatRiskScore, parseImpactValue } from "./case-format";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

interface Props {
  caseId: string;
  // The risk fields of the case, as the case detail holds them.
  risk: RiskFields;
  // The case is closed. The values cannot change, so the edit button is not offered.
  closed: boolean;
  // The caller has cases:triage. Without it the values are shown read-only.
  canWrite: boolean;
  // Called with the stored values, so the case header shows them at once.
  onSaved: (saved: ImpactScores) => void;
}

const RISK_FIELD: Record<ImpactKey, keyof RiskFields> = { c: "impact_c", b: "impact_b", a: "impact_a" };
// The select value that stands for "not set". Every other option is a digit from 1 to 5.
const NOT_SET = "not-set";
const DIGITS = ["1", "2", "3", "4", "5"] as const;
// The stable code of a stale save. The 409 body also carries current_version, which ApiError exposes.
const CONFLICT_CODE = "impact_version_conflict";

export function CaseRiskScore({ caseId, risk, closed, canWrite, onSaved }: Props) {
  const [editing, setEditing] = useState(false);
  const canEdit = canWrite && !closed;

  let description = "The risk score is the mean of the three impact values, to one decimal place. It needs all three.";
  if (closed) description = "This case is closed, so its impact values cannot be changed.";
  else if (!canWrite) description = "The impact values of this case. Your account cannot change them.";

  function applySaved(saved: ImpactScores) {
    setEditing(false);
    toast.success(`Impact values saved as version ${saved.version}.`);
    onSaved(saved);
  }

  return (
    <section aria-labelledby="case-risk-heading" className="mt-6 flex flex-col gap-3 border-t pt-4 text-sm">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id="case-risk-heading" className="font-medium">
          Risk score
        </h3>
        {canEdit && (
          <Button variant="outline" size="sm" onClick={() => setEditing(true)}>
            Edit impact values
          </Button>
        )}
      </div>
      <p className="text-2xl font-semibold tabular-nums">{formatRiskScore(risk.risk_score)}</p>
      <p className="text-muted-foreground">{description}</p>
      <dl className="grid gap-4 sm:grid-cols-3">
        {IMPACT_VALUE_LABELS.map(({ key, label }) => (
          <ValueDetail key={key} label={label} value={risk[RISK_FIELD[key]]} />
        ))}
      </dl>
      {canEdit && (
        <Dialog open={editing} onOpenChange={setEditing}>
          <DialogContent className="max-h-[90vh] overflow-y-auto">
            {/* Mounted only while the dialog is open, so every opening starts from the current version. */}
            <EditRiskForm caseId={caseId} onSaved={applySaved} />
          </DialogContent>
        </Dialog>
      )}
    </section>
  );
}

function ValueDetail({ label, value }: { label: string; value: number | null }) {
  return (
    <div className="grid gap-1">
      <dt className="font-medium">{label}</dt>
      <dd className="text-muted-foreground">{value === null ? "Not set" : value}</dd>
    </div>
  );
}

// The form values as the selects hold them: "" when not set, otherwise a digit.
type FormValues = Record<ImpactKey, string>;

function textOf(value: ImpactValue | null): string {
  return value === null ? "" : String(value);
}

interface FormProps {
  caseId: string;
  onSaved: (saved: ImpactScores) => void;
}

function EditRiskForm({ caseId, onSaved }: FormProps) {
  // The version the editor opened with. A save against a newer version is refused, so nothing is overwritten silently.
  const [current, setCurrent] = useState<ImpactScores | null>(null);
  const [values, setValues] = useState<FormValues | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    let stale = false;
    getImpactScores(caseId)
      .then((result) => {
        if (stale) return;
        setCurrent(result);
        setValues({ c: textOf(result.c), b: textOf(result.b), a: textOf(result.a) });
      })
      .catch((caught: unknown) => {
        if (!stale) setError(describeError(caught));
      });
    return () => {
      stale = true;
    };
  }, [caseId]);

  function chooseValue(key: ImpactKey, value: string) {
    setValues((previous) => previous && { ...previous, [key]: value === NOT_SET ? "" : value });
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (current === null || values === null) return;
    setError(null);
    // Every value is checked before anything is sent. A value outside 1 to 5 is refused here and by the backend.
    const body: Record<ImpactKey, ImpactValue | null> = { c: null, b: null, a: null };
    for (const { key, label } of IMPACT_VALUE_LABELS) {
      const parsed = parseImpactValue(values[key]);
      if ("error" in parsed) {
        setError(`${label}: ${parsed.error}`);
        return;
      }
      body[key] = parsed.value;
    }
    const request: ImpactScoresUpdateBody = { expected_version: current.version, ...body };
    setBusy(true);
    try {
      onSaved(await updateImpactScores(caseId, request));
    } catch (caught) {
      setBusy(false);
      if (caught instanceof ApiError && caught.code === CONFLICT_CODE) {
        const version = caught.currentVersion;
        setError(version === null ? describeError(caught) : `${describeError(caught)} The current version is ${version}.`);
        // Reload the version, so the next save starts from the current one. The chosen values stay.
        getImpactScores(caseId)
          .then(setCurrent)
          .catch((reloadError: unknown) => setError(describeError(reloadError)));
      } else {
        setError(describeError(caught));
      }
    }
  }

  return (
    <>
      <DialogHeader>
        <DialogTitle>Edit impact values</DialogTitle>
        <DialogDescription>
          Saving stores a new version. Earlier versions stay on record.
          {current !== null && (
            <>
              {" "}
              Editing started from version {current.version}
              {current.version === 0 ? " (none recorded yet)" : ""}.
            </>
          )}
        </DialogDescription>
      </DialogHeader>
      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      {values === null && error === null && <p className="text-sm text-muted-foreground">Loading impact values…</p>}
      {values !== null && (
        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <FieldGroup>
            {IMPACT_VALUE_LABELS.map(({ key, label }) => (
              <Field key={key}>
                <FieldLabel htmlFor={`risk-${key}`}>{label}</FieldLabel>
                <Select
                  value={values[key] === "" ? NOT_SET : values[key]}
                  onValueChange={(value) => {
                    if (value) chooseValue(key, value);
                  }}
                >
                  <SelectTrigger id={`risk-${key}`} className="w-full">
                    <SelectValue>
                      {(value: string | null) => (value === null || value === NOT_SET ? "Not set" : value)}
                    </SelectValue>
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value={NOT_SET}>Not set</SelectItem>
                    {DIGITS.map((digit) => (
                      <SelectItem key={digit} value={digit}>
                        {digit}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </Field>
            ))}
          </FieldGroup>
          <DialogFooter>
            <Button type="submit" disabled={busy}>
              {busy ? "Saving…" : "Save new version"}
            </Button>
          </DialogFooter>
        </form>
      )}
    </>
  );
}
