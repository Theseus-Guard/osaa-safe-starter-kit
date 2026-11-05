import { FormEvent, useEffect, useState } from "react";
import { updateSystemTimezone } from "../api";
import { describeError } from "../messages";
import { formatInTimezone } from "../time";
import { TimezonePicker } from "@/components/timezone-picker";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";

interface Props {
  onDone: () => void;
}

// Full-page first-run step for the first admin, shown after login until a system timezone is set.
export function SetupTimezone({ onDone }: Props) {
  const [zone, setZone] = useState<string | null>(null);
  const [now, setNow] = useState(() => new Date());
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (zone === null) return;
    setBusy(true);
    setError(null);
    try {
      await updateSystemTimezone({ timezone: zone, confirm: true, current: null });
      onDone();
    } catch (caught) {
      setError(describeError(caught));
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Choose the system timezone</CardTitle>
        <CardDescription>
          All times in this installation are shown in this timezone; you can change it later from Settings, and every change is
          recorded in the audit log.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <FieldGroup>
            <Field>
              <FieldLabel htmlFor="setup-timezone">
                System timezone <span className="font-normal text-muted-foreground">(required)</span>
              </FieldLabel>
              <TimezonePicker id="setup-timezone" value={zone} onChange={setZone} disabled={busy} />
            </Field>
          </FieldGroup>
          {zone && (
            <p className="text-sm text-muted-foreground">
              Current time in {zone}: {formatInTimezone(now.toISOString(), zone)}
            </p>
          )}
          {error && (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
          <Button type="submit" disabled={zone === null || busy} className="self-start">
            {busy ? "Saving…" : "Save timezone"}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
