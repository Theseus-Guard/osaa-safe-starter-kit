import { EyeIcon, EyeOffIcon, Loader2Icon, OctagonXIcon, TriangleAlertIcon } from "lucide-react";
import { FormEvent, useEffect, useState } from "react";
import { ApiError, readiness, Readiness } from "../api";
import { describeCode, describeError } from "../messages";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";

interface Props {
  notice: string | null;
  onLogin: (username: string, password: string) => Promise<void>;
}

// Caution tone for setup states. It is not a failed sign-in, so it is not destructive red.
const WARNING_ALERT =
  "border-amber-300 bg-amber-50 text-amber-950 *:data-[slot=alert-description]:text-amber-900 " +
  "dark:border-amber-800 dark:bg-amber-950/50 dark:text-amber-100 dark:*:data-[slot=alert-description]:text-amber-200 " +
  "[&>svg]:text-amber-700 dark:[&>svg]:text-amber-300";

interface Failure {
  tone: "error" | "warning";
  title: string;
  message: string;
}

export function Login({ notice, onLogin }: Props) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [busy, setBusy] = useState(false);
  const [state, setState] = useState<Readiness | "checking">("checking");

  useEffect(() => {
    readiness()
      .then(setState)
      .catch(() => setState("unavailable"));
  }, []);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setFailure(null);
    setBusy(true);
    try {
      await onLogin(username, password);
    } catch (caught) {
      // A 503 at login means the system timezone is not set yet, and this non-admin account cannot continue.
      if (caught instanceof ApiError && caught.status === 503) {
        setFailure({ tone: "warning", title: "Setup required", message: describeCode("system setup required") });
      } else {
        setFailure({ tone: "error", title: "Sign-in failed", message: describeError(caught) });
      }
    } finally {
      setBusy(false);
    }
  }

  const unavailable = state === "setup_required" || state === "unavailable";

  return (
    <div className="flex min-h-svh flex-col bg-background">
      <main className="flex flex-1 flex-col items-center justify-center gap-4 p-4 sm:p-6">
        <div className="flex w-full max-w-[400px] flex-col gap-4">
          <Card>
            <CardHeader>
              <div className="flex items-center gap-3">
                <div className="flex size-10 shrink-0 items-center justify-center rounded-lg bg-primary text-primary-foreground" aria-hidden="true">
                  <ShieldMark />
                </div>
                <div className="flex flex-col">
                  <h1 className="text-base leading-tight font-semibold tracking-tight">SAFE-Starter-Kit</h1>
                  <p className="text-sm text-muted-foreground">Sign in to continue</p>
                </div>
              </div>
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              {notice && (
                <Alert>
                  <AlertDescription>{notice}</AlertDescription>
                </Alert>
              )}
              {state === "setup_required" && (
                <Alert className={WARNING_ALERT}>
                  <TriangleAlertIcon />
                  <AlertTitle>Setup required</AlertTitle>
                  <AlertDescription>{describeCode("setup_required")}</AlertDescription>
                </Alert>
              )}
              {state === "unavailable" && (
                <Alert>
                  <OctagonXIcon />
                  <AlertTitle>Service unavailable</AlertTitle>
                  <AlertDescription>The service is not available right now.</AlertDescription>
                </Alert>
              )}
              {failure &&
                (failure.tone === "warning" ? (
                  <Alert className={WARNING_ALERT}>
                    <TriangleAlertIcon />
                    <AlertTitle>{failure.title}</AlertTitle>
                    <AlertDescription>{failure.message}</AlertDescription>
                  </Alert>
                ) : (
                  <Alert variant="destructive">
                    <OctagonXIcon />
                    <AlertTitle>{failure.title}</AlertTitle>
                    <AlertDescription>{failure.message}</AlertDescription>
                  </Alert>
                ))}

              <form onSubmit={handleSubmit} className="flex flex-col gap-4">
                <FieldGroup>
                  <Field>
                    <FieldLabel htmlFor="login-username">Username</FieldLabel>
                    <Input
                      id="login-username"
                      name="username"
                      value={username}
                      onChange={(event) => setUsername(event.target.value)}
                      autoComplete="username"
                      autoFocus
                      required
                      disabled={unavailable}
                    />
                  </Field>
                  <Field>
                    <FieldLabel htmlFor="login-password">Password</FieldLabel>
                    <div className="relative">
                      <Input
                        id="login-password"
                        name="password"
                        type={showPassword ? "text" : "password"}
                        value={password}
                        onChange={(event) => setPassword(event.target.value)}
                        autoComplete="current-password"
                        required
                        disabled={unavailable}
                        className="pr-10"
                      />
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon-sm"
                        className="absolute top-1/2 right-1 -translate-y-1/2"
                        aria-label={showPassword ? "Hide password" : "Show password"}
                        aria-pressed={showPassword}
                        disabled={unavailable}
                        onClick={() => setShowPassword((value) => !value)}
                      >
                        {showPassword ? <EyeOffIcon /> : <EyeIcon />}
                      </Button>
                    </div>
                  </Field>
                </FieldGroup>
                <Button type="submit" className="w-full" disabled={busy || unavailable} aria-busy={busy}>
                  {busy && <Loader2Icon className="animate-spin" />}
                  {busy ? "Logging in…" : "Log in"}
                </Button>
              </form>
            </CardContent>
          </Card>
          <p className="text-center text-xs text-muted-foreground">Authorized users only.</p>
        </div>
      </main>
      <footer className="px-4 py-6 text-center text-xs text-muted-foreground">
        <nav className="flex flex-wrap justify-center gap-x-4 gap-y-1">
          <a className="underline underline-offset-4 hover:text-foreground" href="https://github.com/OpenSecureAIAlliance/wg-safe" target="_blank" rel="noopener noreferrer">
            Open Secure AI Alliance official repo
          </a>
          <a className="underline underline-offset-4 hover:text-foreground" href="https://secureaialliance.org/" target="_blank" rel="noopener noreferrer">
            Open Secure AI Alliance (OSAA)
          </a>
        </nav>
        <p className="mt-2">SAFE-Starter-Kit is an independent open-source project. It is not an official Alliance product.</p>
      </footer>
    </div>
  );
}

// Simple shield outline. Drawn inline so no external image is loaded.
function ShieldMark() {
  return (
    <svg viewBox="0 0 24 24" className="size-6" fill="none" stroke="currentColor" strokeWidth={2} strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 3 4.5 5.8v5.4c0 4.5 3.1 8.2 7.5 9.8 4.4-1.6 7.5-5.3 7.5-9.8V5.8L12 3Z" />
      <path d="m9 12 2 2 4-4" />
    </svg>
  );
}
