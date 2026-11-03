import { useEffect, useRef, useState } from "react";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { changePassword, demoSignIn, login, logout, me, Me, onSessionEnded, restoreSession } from "./api";
import { watchIdle } from "./idle";
import { ChangePassword } from "./screens/ChangePassword";
import { Home } from "./screens/Home";
import { Login } from "./screens/Login";
import { SetupTimezone } from "./screens/SetupTimezone";

// "starting" is shown until the start-up check has answered, so the login page does not flash before a valid session loads.
type Phase = "starting" | "anonymous" | "password-change" | "timezone-setup" | "signed-in";

// The first-login steps come before the dashboard. The forced password change is checked first.
function phaseFor(current: Me): Phase {
  if (current.password_change_required) return "password-change";
  if (current.timezone_setup_required) return "timezone-setup";
  return "signed-in";
}

export function App() {
  const [phase, setPhase] = useState<Phase>("starting");
  const [profile, setProfile] = useState<Me | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [idleWarning, setIdleWarning] = useState(false);
  const startedRef = useRef(false);

  useEffect(() => {
    onSessionEnded(() => {
      setProfile(null);
      setPhase("anonymous");
      setNotice("Your session has ended. Please log in again.");
    });
  }, []);

  // A reload loses the in-memory state, but the HttpOnly cookies may still hold a session. Development runs effects
  // twice (StrictMode), so the check runs once.
  useEffect(() => {
    if (startedRef.current) return;
    startedRef.current = true;
    restoreSession()
      .then((current) => {
        if (current) {
          setProfile(current);
          setPhase(phaseFor(current));
        } else {
          setPhase("anonymous");
        }
      })
      .catch(() => setPhase("anonymous"));
  }, []);

  // Signs the user out after an hour without activity. The server-side session is ended too, through logout().
  useEffect(() => {
    if (phase !== "signed-in") return;
    return watchIdle({
      onWarning: setIdleWarning,
      onExpired: () => {
        logout()
          .catch(() => undefined)
          .then(() => {
            setIdleWarning(false);
            setProfile(null);
            setPhase("anonymous");
            setNotice("Your session ended after 60 minutes without activity. Please log in again.");
          });
      },
    });
  }, [phase]);

  // Shared by the password sign-in and the demo sign-in: both leave a session cookie behind.
  async function completeSignIn() {
    const current = await me();
    setProfile(current);
    setPhase(phaseFor(current));
  }

  async function handleLogin(username: string, password: string) {
    setNotice(null);
    await login(username, password);
    await completeSignIn();
  }

  async function handleDemoSignIn(username: string, credential: string) {
    setNotice(null);
    await demoSignIn(username, credential);
    await completeSignIn();
  }

  // Shared by the forced first-login change and the voluntary change: the backend ends all sessions,
  // so the user goes back to the login screen.
  function handlePasswordChanged() {
    setProfile(null);
    setPhase("anonymous");
    setNotice("Password changed. Log in with your new password.");
  }

  // The first admin has chosen the system timezone. The session stays valid, so go straight to the dashboard.
  function handleTimezoneSet() {
    setProfile((previous) => (previous ? { ...previous, timezone_setup_required: false } : previous));
    setPhase("signed-in");
  }

  async function handleLogout() {
    try {
      await logout();
    } finally {
      setProfile(null);
      setPhase("anonymous");
      setNotice("You have been logged out.");
    }
  }

  if (phase === "signed-in" && profile) {
    return (
      <>
        {idleWarning && (
          <Alert className="fixed right-4 bottom-4 z-50 w-auto max-w-sm bg-background shadow-lg">
            <AlertDescription>Your session will end within 5 minutes due to inactivity.</AlertDescription>
          </Alert>
        )}
        <Home profile={profile} notice={notice} onLogout={handleLogout} onPasswordChanged={handlePasswordChanged} />
      </>
    );
  }

  if (phase === "starting") return <p className="p-4 text-sm text-muted-foreground">Loading…</p>;

  // The login page has its own full-page layout, with its own header and footer.
  if (phase === "anonymous") return <Login notice={notice} onLogin={handleLogin} onDemoSignIn={handleDemoSignIn} />;

  // The first-login steps stay outside the dashboard shell (no clock and no user menu here).
  return (
    <div className="flex min-h-svh flex-col">
      <header className="flex h-14 items-center border-b bg-background px-4">
        <h1 className="text-base font-semibold tracking-tight">SAFE-Starter-Kit</h1>
      </header>
      <main className="mx-auto flex w-full max-w-md flex-1 flex-col gap-6 p-4 pt-8">
        {phase === "password-change" && profile && (
          <ChangePassword
            heading="Choose a new password"
            description="Your account was created or reset by an administrator. You must set your own password before continuing."
            onChange={changePassword}
            onDone={handlePasswordChanged}
          />
        )}
        {phase === "timezone-setup" && profile && <SetupTimezone onDone={handleTimezoneSet} />}
      </main>
    </div>
  );
}
