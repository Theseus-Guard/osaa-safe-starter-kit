import { FormEvent, useEffect, useState } from "react";
import { readiness, Readiness } from "../api";
import { describeCode, describeError } from "../messages";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";

interface Props {
  notice: string | null;
  onLogin: (username: string, password: string) => Promise<void>;
}

export function Login({ notice, onLogin }: Props) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [state, setState] = useState<Readiness | "checking">("checking");

  useEffect(() => {
    readiness()
      .then(setState)
      .catch(() => setState("unavailable"));
  }, []);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await onLogin(username, password);
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  const unavailable = state === "setup_required" || state === "unavailable";

  return (
    <Card>
      <CardHeader>
        <CardTitle>Log in</CardTitle>
      </CardHeader>
      <CardContent>
        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          {notice && (
            <Alert>
              <AlertDescription>{notice}</AlertDescription>
            </Alert>
          )}
          {state === "setup_required" && (
            <Alert>
              <AlertDescription>{describeCode("setup_required")}</AlertDescription>
            </Alert>
          )}
          {state === "unavailable" && (
            <Alert>
              <AlertDescription>The service is not available right now.</AlertDescription>
            </Alert>
          )}
          <FieldGroup>
            <Field>
              <FieldLabel htmlFor="login-username">Username</FieldLabel>
              <Input
                id="login-username"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                autoComplete="username"
                required
                disabled={unavailable}
              />
            </Field>
            <Field>
              <FieldLabel htmlFor="login-password">Password</FieldLabel>
              <Input
                id="login-password"
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete="current-password"
                required
                disabled={unavailable}
              />
            </Field>
          </FieldGroup>
          {error && (
            <Alert variant="destructive">
              <AlertDescription>{error}</AlertDescription>
            </Alert>
          )}
          <Button type="submit" className="w-full" disabled={busy || unavailable}>
            {busy ? "Logging in…" : "Log in"}
          </Button>
        </form>
      </CardContent>
    </Card>
  );
}
