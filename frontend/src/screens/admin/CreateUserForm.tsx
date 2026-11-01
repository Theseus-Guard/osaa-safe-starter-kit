import { FormEvent, useState } from "react";
import { createUser, CreatedUser, Role, ROLES } from "../../api";
import { describeError } from "../../messages";
import { MIN_PASSWORD_LENGTH } from "../ChangePassword";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";

interface Props {
  onCreated: () => void;
}

export function CreateUserForm({ onCreated }: Props) {
  const [username, setUsername] = useState("");
  const [role, setRole] = useState<Role>("reporter");
  const [temporary, setTemporary] = useState("");
  const [created, setCreated] = useState<CreatedUser | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setCreated(null);
    if (temporary.length < MIN_PASSWORD_LENGTH) {
      setError(`The temporary password must be at least ${MIN_PASSWORD_LENGTH} characters.`);
      return;
    }
    setBusy(true);
    try {
      setCreated(await createUser(username, role, temporary));
      setUsername("");
      setTemporary("");
      onCreated();
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Create user</CardTitle>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <FieldGroup className="grid gap-4 sm:grid-cols-3">
            <Field>
              <FieldLabel htmlFor="create-username">Username</FieldLabel>
              <Input id="create-username" value={username} onChange={(e) => setUsername(e.target.value)} required />
            </Field>
            <Field>
              <FieldLabel htmlFor="create-role">Role</FieldLabel>
              <Select
                value={role}
                onValueChange={(value) => {
                  if (value) setRole(value as Role);
                }}
              >
                <SelectTrigger id="create-role" className="w-full">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {ROLES.map((r) => (
                    <SelectItem key={r} value={r}>
                      {r}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </Field>
            <Field>
              <FieldLabel htmlFor="create-temporary">Temporary password</FieldLabel>
              <Input
                id="create-temporary"
                type="password"
                value={temporary}
                onChange={(e) => setTemporary(e.target.value)}
                autoComplete="new-password"
                required
              />
            </Field>
          </FieldGroup>
          {error && (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
          <Button type="submit" disabled={busy} className="self-start">
            Create user
          </Button>
        </form>
        {created && (
          <Alert>
            <AlertDescription>
              Created {created.username} ({created.role}). User ID: <code className="font-mono text-xs">{created.id}</code>. Share the
              temporary password through a secure channel. The user must change it at first login.
            </AlertDescription>
          </Alert>
        )}
      </CardContent>
    </Card>
  );
}
