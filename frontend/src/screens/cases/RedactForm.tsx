import { FormEvent, useState } from "react";
import { toast } from "sonner";
import { Derivation, RedactionRangeBody, TLP_LABELS, TlpLabel, redactEvidence } from "../../api";
import { describeError } from "../../messages";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";

// The same limits as the API (safe_kit.redaction). The server checks them again and refuses overlaps and bad labels.
export const REDACTION_MAX_RANGES = 50;
export const REDACTION_REASON_LIMIT = 500;

interface Range {
  key: number;
  from: string;
  to: string;
  reason: string;
}

// Returns the ranges as the API takes them, or a message for the first one that is not valid.
export function checkRanges(ranges: Range[]): { removed: RedactionRangeBody[] } | { problem: string } {
  if (ranges.length === 0) return { problem: "Add at least one line range to remove." };
  if (ranges.length > REDACTION_MAX_RANGES) return { problem: `At most ${REDACTION_MAX_RANGES} ranges are allowed.` };
  const removed: RedactionRangeBody[] = [];
  for (const [index, range] of ranges.entries()) {
    const from = Number(range.from);
    const to = range.to.trim() === "" ? from : Number(range.to);
    const position = `Range ${index + 1}`;
    if (!Number.isInteger(from) || from < 1 || !Number.isInteger(to) || to < from) {
      return { problem: `${position}: enter line numbers from 1, with the first not after the last.` };
    }
    const reason = range.reason.trim();
    if (reason === "" || reason.length > REDACTION_REASON_LIMIT) {
      return { problem: `${position}: enter a reason of 1 to ${REDACTION_REASON_LIMIT} characters.` };
    }
    removed.push({ from_line: from, to_line: to, reason });
  }
  return { removed };
}

interface Props {
  evidenceId: string;
  onRedacted: (derivation: Derivation) => void;
}

// Needs evidence:write (and evidence:read, which the detail view already has). Makes a redacted copy of a text/plain item.
// The original is never changed. A copy may not be labelled less restrictively than its original; the server refuses that.
export function RedactForm({ evidenceId, onRedacted }: Props) {
  const [nextKey, setNextKey] = useState(2);
  const [ranges, setRanges] = useState<Range[]>([{ key: 1, from: "", to: "", reason: "" }]);
  const [tlpLabel, setTlpLabel] = useState<TlpLabel>("AMBER");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function update(key: number, field: "from" | "to" | "reason", value: string) {
    setRanges((current) => current.map((range) => (range.key === key ? { ...range, [field]: value } : range)));
  }

  function addRange() {
    setRanges((current) => [...current, { key: nextKey, from: "", to: "", reason: "" }]);
    setNextKey((value) => value + 1);
  }

  function removeRange(key: number) {
    setRanges((current) => (current.length > 1 ? current.filter((range) => range.key !== key) : current));
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const checked = checkRanges(ranges);
    if ("problem" in checked) {
      setError(checked.problem);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const derivation = await redactEvidence(evidenceId, { removed: checked.removed, tlp_label: tlpLabel });
      toast.success("Redacted copy stored. The original is unchanged.");
      setRanges([{ key: nextKey, from: "", to: "", reason: "" }]);
      setNextKey((value) => value + 1);
      onRedacted(derivation);
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form className="flex flex-col gap-4" aria-label="Make a redacted copy" onSubmit={(event) => void handleSubmit(event)}>
      <p className="text-sm text-muted-foreground">
        Remove whole lines, one reason for each range. The copy shows [REDACTED] in their place and keeps the reason; the
        removed text is not kept in the manifest.
      </p>
      {ranges.map((range, index) => (
        <div key={range.key} className="grid gap-3 rounded-md border p-3 sm:grid-cols-[6rem_6rem_1fr_auto] sm:items-end">
          <Field>
            <FieldLabel htmlFor={`redact-from-${range.key}`}>From line</FieldLabel>
            <Input
              id={`redact-from-${range.key}`}
              type="number"
              min={1}
              inputMode="numeric"
              value={range.from}
              onChange={(event) => update(range.key, "from", event.target.value)}
            />
          </Field>
          <Field>
            <FieldLabel htmlFor={`redact-to-${range.key}`}>To line</FieldLabel>
            <Input
              id={`redact-to-${range.key}`}
              type="number"
              min={1}
              inputMode="numeric"
              value={range.to}
              onChange={(event) => update(range.key, "to", event.target.value)}
            />
          </Field>
          <Field>
            <FieldLabel htmlFor={`redact-reason-${range.key}`}>Reason for range {index + 1}</FieldLabel>
            <Input
              id={`redact-reason-${range.key}`}
              value={range.reason}
              onChange={(event) => update(range.key, "reason", event.target.value)}
            />
          </Field>
          <Button type="button" variant="outline" size="sm" disabled={ranges.length === 1} onClick={() => removeRange(range.key)}>
            Remove range
          </Button>
        </div>
      ))}
      <div>
        <Button type="button" variant="outline" size="sm" onClick={addRange} disabled={ranges.length >= REDACTION_MAX_RANGES}>
          Add range
        </Button>
      </div>
      <Field>
        <FieldLabel htmlFor={`redact-tlp-${evidenceId}`}>TLP label of the copy</FieldLabel>
        <select
          id={`redact-tlp-${evidenceId}`}
          className="h-9 w-full rounded-md border border-input bg-background px-3 text-sm"
          value={tlpLabel}
          onChange={(event) => setTlpLabel(event.target.value as TlpLabel)}
        >
          {TLP_LABELS.map((label) => (
            <option key={label} value={label}>
              {label}
            </option>
          ))}
        </select>
        <FieldDescription>
          The copy may not be labelled less restrictively than the original. The server refuses a label that is wider.
        </FieldDescription>
      </Field>
      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      <div>
        <Button type="submit" disabled={busy}>
          Make redacted copy
        </Button>
      </div>
    </form>
  );
}
