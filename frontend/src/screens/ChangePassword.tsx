import { FormEvent, useState } from "react";
import { describeError } from "../messages";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";

export const MIN_PASSWORD_LENGTH = 12;

interface Props {
  heading: string;
  description?: string;
  onChange: (currentPassword: string, newPassword: string) => Promise<void>;
  onDone: () => void;
}

export function ChangePassword({ heading, description, onChange, onDone }: Props) {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  // The field the current error is about, so that the field can point at the message.
  const [errorField, setErrorField] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setErrorField(null);
    if (next.length < MIN_PASSWORD_LENGTH) {
      setErrorField("new-password");
      setError(`The new password must be at least ${MIN_PASSWORD_LENGTH} characters.`);
      return;
    }
    if (next !== confirm) {
      setErrorField("confirm-password");
      setError("The new passwords do not match.");
      return;
    }
    setBusy(true);
    try {
      await onChange(current, next);
      onDone();
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>{heading}</CardTitle>
        {description && <CardDescription>{description}</CardDescription>}
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <FieldGroup>
            <Field>
              <FieldLabel htmlFor="current-password">
                Current password <span className="font-normal text-muted-foreground">(required)</span>
              </FieldLabel>
              <Input
                id="current-password"
                type="password"
                value={current}
                onChange={(e) => setCurrent(e.target.value)}
                autoComplete="current-password"
                required
              />
            </Field>
            <Field>
              <FieldLabel htmlFor="new-password">
                New password <span className="font-normal text-muted-foreground">(required)</span>
              </FieldLabel>
              <Input
                id="new-password"
                aria-invalid={errorField === "new-password" || undefined}
                aria-describedby={errorField === "new-password" ? "change-password-error new-password-hint" : "new-password-hint"}
                type="password"
                value={next}
                onChange={(e) => setNext(e.target.value)}
                autoComplete="new-password"
                required
              />
              <FieldDescription id="new-password-hint">
                At least {MIN_PASSWORD_LENGTH} characters. Changing it ends all of your sessions.
              </FieldDescription>
            </Field>
            <Field>
              <FieldLabel htmlFor="confirm-password">
                Confirm new password <span className="font-normal text-muted-foreground">(required)</span>
              </FieldLabel>
              <Input
                id="confirm-password"
                aria-invalid={errorField === "confirm-password" || undefined}
                aria-describedby={errorField === "confirm-password" ? "change-password-error" : undefined}
                type="password"
                value={confirm}
                onChange={(e) => setConfirm(e.target.value)}
                autoComplete="new-password"
                required
              />
            </Field>
          </FieldGroup>
          {error && (
            <Alert variant="destructive" id="change-password-error">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
          <Button type="submit" disabled={busy} className="self-start">
            {busy ? "Saving…" : "Change password"}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
