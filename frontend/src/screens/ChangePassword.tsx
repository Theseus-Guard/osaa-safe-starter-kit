import { FormEvent, useState } from "react";
import { describeError } from "../messages";

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
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (next.length < MIN_PASSWORD_LENGTH) {
      setError(`The new password must be at least ${MIN_PASSWORD_LENGTH} characters.`);
      return;
    }
    if (next !== confirm) {
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
    <section className="card">
      <h2>{heading}</h2>
      {description && <p>{description}</p>}
      <form onSubmit={handleSubmit}>
        <label>
          Current password
          <input type="password" value={current} onChange={(e) => setCurrent(e.target.value)} autoComplete="current-password" required />
        </label>
        <label>
          New password
          <input type="password" value={next} onChange={(e) => setNext(e.target.value)} autoComplete="new-password" required />
        </label>
        <label>
          Confirm new password
          <input type="password" value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="new-password" required />
        </label>
        <p className="hint">At least {MIN_PASSWORD_LENGTH} characters. Changing it ends all of your sessions.</p>
        {error && <p className="error">{error}</p>}
        <button type="submit" disabled={busy}>
          {busy ? "Saving…" : "Change password"}
        </button>
      </form>
    </section>
  );
}
