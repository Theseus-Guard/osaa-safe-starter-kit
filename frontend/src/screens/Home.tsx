import { changePassword, Me } from "../api";
import { useHashPage } from "../routes";
import { AppShell } from "./AppShell";
import { ChangePassword } from "./ChangePassword";
import { Overview } from "./Overview";
import { RolesMatrix } from "./admin/RolesMatrix";
import { PrivacyPage } from "./admin/PrivacyPage";
import { UsersPage } from "./admin/UsersPage";

interface Props {
  profile: Me;
  notice: string | null;
  onLogout: () => void;
  onPasswordChanged: () => void;
}

export function Home({ profile, notice, onLogout, onPasswordChanged }: Props) {
  const isAdmin = profile.role === "admin";
  const page = useHashPage(isAdmin);

  return (
    <AppShell profile={profile} page={page} onLogout={onLogout}>
      {page === "overview" && <Overview profile={profile} notice={notice} />}
      {page === "users" && isAdmin && <UsersPage currentUserId={profile.id} />}
      {page === "privacy" && isAdmin && <PrivacyPage />}
      {page === "roles" && isAdmin && <RolesMatrix editable />}
      {page === "account" && (
        <ChangePassword
          heading="Change your password"
          description="This ends all of your sessions. You will need to log in again."
          onChange={changePassword}
          onDone={onPasswordChanged}
        />
      )}
    </AppShell>
  );
}
