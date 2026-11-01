import { FormEvent, useState } from "react";
import { createUser, CreatedUser, Role, ROLES } from "../../api";
import { describeError } from "../../messages";
import { MIN_PASSWORD_LENGTH } from "../ChangePassword";

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
    <section className="card">
      <h2>Create user</h2>
      <form onSubmit={handleSubmit}>
        <label>
          Username
          <input value={username} onChange={(e) => setUsername(e.target.value)} required />
        </label>
        <label>
          Role
          <select value={role} onChange={(e) => setRole(e.target.value as Role)}>
            {ROLES.map((r) => (
              <option key={r} value={r}>
                {r}
              </option>
            ))}
          </select>
        </label>
        <label>
          Temporary password
          <input type="password" value={temporary} onChange={(e) => setTemporary(e.target.value)} autoComplete="new-password" required />
        </label>
        {error && <p className="error">{error}</p>}
        <button type="submit" disabled={busy}>
          Create user
        </button>
      </form>
      {created && (
        <p className="notice">
          Created {created.username} ({created.role}). User ID: <code>{created.id}</code>. Share the temporary password through a
          secure channel. The user must change it at first login.
        </p>
      )}
    </section>
  );
}
