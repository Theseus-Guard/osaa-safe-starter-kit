import { useEffect, useState } from "react";
import { changePassword, login, logout, me, Me, onSessionEnded } from "./api";
import { ChangePassword } from "./screens/ChangePassword";
import { Home } from "./screens/Home";
import { Login } from "./screens/Login";
import { SetupTimezone } from "./screens/SetupTimezone";

type Phase = "anonymous" | "password-change" | "timezone-setup" | "signed-in";

export function App() {
  const [phase, setPhase] = useState<Phase>("anonymous");
  const [profile, setProfile] = useState<Me | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    onSessionEnded(() => {
      setProfile(null);
      setPhase("anonymous");
      setNotice("Your session has ended. Please log in again.");
    });
  }, []);

  async function handleLogin(username: string, password: string) {
    setNotice(null);
    await login(username, password);
    const current = await me();
    setProfile(current);
    if (current.password_change_required) setPhase("password-change");
    else if (current.timezone_setup_required) setPhase("timezone-setup");
    else setPhase("signed-in");
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
    return <Home profile={profile} notice={notice} onLogout={handleLogout} onPasswordChanged={handlePasswordChanged} />;
  }

  // Login and the first-login steps stay outside the dashboard shell (no clock and no user menu here).
  return (
    <div className="flex min-h-svh flex-col">
      <header className="flex h-14 items-center border-b bg-background px-4">
        <h1 className="text-base font-semibold tracking-tight">SAFE-Starter-Kit</h1>
      </header>
      <main className="mx-auto flex w-full max-w-md flex-1 flex-col gap-6 p-4 pt-8">
        {phase === "anonymous" && <Login notice={notice} onLogin={handleLogin} />}
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
