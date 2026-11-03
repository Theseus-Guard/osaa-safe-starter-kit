import { FormEvent, useState } from "react";
import { createUser, CreatedUser, NewUser, Role, ROLES } from "../../api";
import { describeError } from "../../messages";
import { EMAIL_REQUIRED_MESSAGE } from "../../profile";
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
  const [email, setEmail] = useState("");
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [title, setTitle] = useState("");
  const [role, setRole] = useState<Role>("reporter");
  const [temporary, setTemporary] = useState("");
  const [created, setCreated] = useState<CreatedUser | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The field the current error is about, so that the field can point at the message.
  const [errorField, setErrorField] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setErrorField(null);
    setCreated(null);
    if (email.trim() === "") {
      setErrorField("create-email");
      setError(EMAIL_REQUIRED_MESSAGE);
      return;
    }
    if (temporary.length < MIN_PASSWORD_LENGTH) {
      setErrorField("create-temporary");
      setError(`The temporary password must be at least ${MIN_PASSWORD_LENGTH} characters.`);
      return;
    }
    // Optional fields are sent only when filled in.
    const body: NewUser = { username, role, temporary_password: temporary, email: email.trim() };
    if (firstName.trim() !== "") body.first_name = firstName.trim();
    if (lastName.trim() !== "") body.last_name = lastName.trim();
    if (title.trim() !== "") body.title = title.trim();
    setBusy(true);
    try {
      setCreated(await createUser(body));
      setUsername("");
      setEmail("");
      setFirstName("");
      setLastName("");
      setTitle("");
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
          <FieldGroup className="grid gap-4 sm:grid-cols-2">
            <Field>
              <FieldLabel htmlFor="create-username">
                Username <span className="font-normal text-muted-foreground">(required)</span>
              </FieldLabel>
              <Input id="create-username" value={username} onChange={(e) => setUsername(e.target.value)} required />
            </Field>
            <Field>
              <FieldLabel htmlFor="create-email">
                Email <span className="font-normal text-muted-foreground">(required)</span>
              </FieldLabel>
              <Input
                id="create-email"
                aria-invalid={errorField === "create-email" || undefined}
                aria-describedby={errorField === "create-email" ? "create-user-error" : undefined}
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                autoComplete="off"
                required
              />
            </Field>
            <Field>
              <FieldLabel htmlFor="create-first-name">First name</FieldLabel>
              <Input id="create-first-name" value={firstName} onChange={(e) => setFirstName(e.target.value)} />
            </Field>
            <Field>
              <FieldLabel htmlFor="create-last-name">Last name</FieldLabel>
              <Input id="create-last-name" value={lastName} onChange={(e) => setLastName(e.target.value)} />
            </Field>
            <Field className="sm:col-span-2">
              <FieldLabel htmlFor="create-title">Title</FieldLabel>
              <Input id="create-title" value={title} onChange={(e) => setTitle(e.target.value)} />
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
              <FieldLabel htmlFor="create-temporary">
                Temporary password <span className="font-normal text-muted-foreground">(required)</span>
              </FieldLabel>
              <Input
                id="create-temporary"
                aria-invalid={errorField === "create-temporary" || undefined}
                aria-describedby={errorField === "create-temporary" ? "create-user-error" : undefined}
                type="password"
                value={temporary}
                onChange={(e) => setTemporary(e.target.value)}
                autoComplete="new-password"
                required
              />
            </Field>
          </FieldGroup>
          {error && (
            <Alert variant="destructive" id="create-user-error">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
          <Button type="submit" disabled={busy} className="self-start">
            Create user
          </Button>
        </form>
        {created && (
          <Alert role="status">
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
