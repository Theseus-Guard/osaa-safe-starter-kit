import { useState } from "react";
import { changePassword, Me } from "../api";
import { AdminPanel } from "./AdminPanel";
import { ChangePassword } from "./ChangePassword";

interface Props {
  profile: Me;
  notice: string | null;
  onLogout: () => void;
  onPasswordChanged: () => void;
}

export function Home({ profile, notice, onLogout, onPasswordChanged }: Props) {
  const [changing, setChanging] = useState(false);

  return (
    <>
      <section className="card">
        <h2>Signed in</h2>
        {notice && <p className="notice">{notice}</p>}
        <dl>
          <dt>Username</dt>
          <dd>{profile.username}</dd>
          <dt>Role</dt>
          <dd>{profile.role}</dd>
        </dl>
        <div className="row">
          <button type="button" onClick={() => setChanging((value) => !value)}>
            {changing ? "Cancel" : "Change password"}
          </button>
          <button type="button" className="secondary" onClick={onLogout}>
            Log out
          </button>
        </div>
      </section>
      {changing && (
        <ChangePassword
          heading="Change your password"
          description="This ends all of your sessions. You will need to log in again."
          onChange={changePassword}
          onDone={onPasswordChanged}
        />
      )}
      {profile.role === "admin" && <AdminPanel currentUserId={profile.id} currentRole={profile.role} />}
    </>
  );
}
