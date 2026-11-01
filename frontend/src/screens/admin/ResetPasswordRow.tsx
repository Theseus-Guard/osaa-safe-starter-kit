import { FormEvent, useState } from "react";
import { resetUserPassword } from "../../api";
import { describeError } from "../../messages";
import { MIN_PASSWORD_LENGTH } from "../ChangePassword";

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
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (temporary.length < MIN_PASSWORD_LENGTH) {
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
    <tr>
      <td colSpan={colSpan}>
        <form onSubmit={handleSubmit} aria-label={`Reset password for ${username}`}>
          <label>
            New temporary password for {username}
            <input type="password" value={temporary} onChange={(e) => setTemporary(e.target.value)} autoComplete="new-password" required />
          </label>
          <p className="hint">
            At least {MIN_PASSWORD_LENGTH} characters. All of the user's sessions end, and they must change the password at next login.
          </p>
          {error && <p className="error">{error}</p>}
          <div className="row">
            <button type="submit" disabled={busy}>
              Set temporary password
            </button>
            <button type="button" className="secondary" onClick={onCancel} disabled={busy}>
              Cancel
            </button>
          </div>
        </form>
      </td>
    </tr>
  );
}
