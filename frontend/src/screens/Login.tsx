import { FormEvent, useEffect, useState } from "react";
import { readiness, Readiness } from "../api";
import { describeCode, describeError } from "../messages";

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
    <section className="card">
      <h2>Log in</h2>
      {notice && <p className="notice">{notice}</p>}
      {state === "setup_required" && (
        <p className="warning">{describeCode("setup_required")}</p>
      )}
      {state === "unavailable" && <p className="warning">The service is not available right now.</p>}
      <form onSubmit={handleSubmit}>
        <label>
          Username
          <input
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoComplete="username"
            required
            disabled={unavailable}
          />
        </label>
        <label>
          Password
          <input
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete="current-password"
            required
            disabled={unavailable}
          />
        </label>
        {error && <p className="error">{error}</p>}
        <button type="submit" disabled={busy || unavailable}>
          {busy ? "Logging in…" : "Log in"}
        </button>
      </form>
    </section>
  );
}
