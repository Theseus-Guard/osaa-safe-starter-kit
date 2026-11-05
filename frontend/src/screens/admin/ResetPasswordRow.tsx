import { FormEvent, useState } from "react";
import { resetUserPassword } from "../../api";
import { describeError } from "../../messages";
import { MIN_PASSWORD_LENGTH } from "../ChangePassword";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Field, FieldDescription, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { TableCell, TableRow } from "@/components/ui/table";

interface Props {
  userId: string;
  username: string;
  colSpan: number;
  onDone: (username: string) => void;
  onCancel: () => void;
}

// Inline row under a user in the table. It is rendered only while that user's reset is open.
export function ResetPasswordRow({ userId, username, colSpan, onDone, onCancel }: Props) {
  const [temporary, setTemporary] = useState("");
  const [error, setError] = useState<string | null>(null);
  // The field the current error is about, so that the field can point at the message.
  const [errorField, setErrorField] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const inputId = `reset-password-${userId}`;

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setErrorField(null);
    if (temporary.length < MIN_PASSWORD_LENGTH) {
      setErrorField(inputId);
      setError(`The temporary password must be at least ${MIN_PASSWORD_LENGTH} characters.`);
      return;
    }
    setBusy(true);
    try {
      await resetUserPassword(userId, temporary);
      onDone(username);
    } catch (caught) {
      setError(describeError(caught));
      setBusy(false);
    }
  }

  return (
    <TableRow className="hover:bg-transparent">
      <TableCell colSpan={colSpan} className="whitespace-normal bg-muted/40">
        <form onSubmit={handleSubmit} aria-label={`Reset password for ${username}`} className="flex max-w-md flex-col gap-4">
          <FieldGroup>
            <Field>
              <FieldLabel htmlFor={inputId}>
                New temporary password for {username} <span className="font-normal text-muted-foreground">(required)</span>
              </FieldLabel>
              <Input
                id={inputId}
                aria-invalid={errorField === inputId || undefined}
                aria-describedby={errorField === inputId ? `${inputId}-error` : undefined}
                type="password"
                value={temporary}
                onChange={(e) => setTemporary(e.target.value)}
                autoComplete="new-password"
                required
              />
              <FieldDescription>
                At least {MIN_PASSWORD_LENGTH} characters. All of the user's sessions end, and they must change the password at next
                login.
              </FieldDescription>
            </Field>
          </FieldGroup>
          {error && (
            <Alert variant="destructive" id={`${inputId}-error`}>
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
          <div className="flex gap-2">
            <Button type="submit" disabled={busy}>
              Set temporary password
            </Button>
            <Button type="button" variant="outline" onClick={onCancel} disabled={busy}>
              Cancel
            </Button>
          </div>
        </form>
      </TableCell>
    </TableRow>
  );
}
