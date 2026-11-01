import { Alert, AlertDescription } from "@/components/ui/alert";
import { changePassword, Me } from "../api";
import { useHashPage } from "../routes";
import { SystemTimezoneProvider, useSystemTimezone } from "../system-timezone";
import { AppShell } from "./AppShell";
import { ChangePassword } from "./ChangePassword";
import { Overview } from "./Overview";
import { PrivacyPage } from "./admin/PrivacyPage";
import { RolesMatrix } from "./admin/RolesMatrix";
import { SettingsPage } from "./admin/SettingsPage";
import { UsersPage } from "./admin/UsersPage";

interface Props {
  profile: Me;
  notice: string | null;
  onLogout: () => void;
  onPasswordChanged: () => void;
}

export function Home(props: Props) {
  return (
    <SystemTimezoneProvider>
      <HomeView {...props} />
    </SystemTimezoneProvider>
  );
}

function HomeView({ profile, notice, onLogout, onPasswordChanged }: Props) {
  const isAdmin = profile.role === "admin";
  const page = useHashPage(isAdmin);
  const { error } = useSystemTimezone();

  return (
    <AppShell profile={profile} page={page} onLogout={onLogout}>
      {error && (
        <Alert variant="destructive">
          <AlertDescription>Times are shown in UTC until the system timezone loads. {error}</AlertDescription>
        </Alert>
      )}
      {page === "overview" && <Overview profile={profile} notice={notice} />}
      {page === "users" && isAdmin && <UsersPage currentUserId={profile.id} />}
      {page === "privacy" && isAdmin && <PrivacyPage />}
      {page === "roles" && isAdmin && <RolesMatrix editable />}
      {page === "settings" && isAdmin && <SettingsPage />}
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
