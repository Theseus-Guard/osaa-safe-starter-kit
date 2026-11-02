import { Alert, AlertDescription } from "@/components/ui/alert";
import { changePassword, Me } from "../api";
import { useHashRoute } from "../routes";
import { SystemTimezoneProvider, useSystemTimezone } from "../system-timezone";
import { AppShell } from "./AppShell";
import { ChangePassword } from "./ChangePassword";
import { ProfileCard } from "./ProfileCard";
import { Overview } from "./Overview";
import { PrivacyPage } from "./admin/PrivacyPage";
import { RolesMatrix } from "./admin/RolesMatrix";
import { SettingsPage } from "./admin/SettingsPage";
import { UsersPage } from "./admin/UsersPage";
import { CaseDetailPage } from "./cases/CaseDetailPage";
import { CasesPage } from "./cases/CasesPage";
import { CatalogEntryPage } from "./catalog/CatalogEntryPage";
import { CatalogPage } from "./catalog/CatalogPage";

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
  const { page, caseId, entryId } = useHashRoute(isAdmin);
  const { error } = useSystemTimezone();
  const caseDetailHeading =
    page === "cases" && caseId !== null
      ? { title: "Case", description: "Case details. Times are shown in the system timezone." }
      : page === "catalog" && entryId !== null
        ? { title: "Catalog entry", description: "One catalog entry: its versions, verification history and the forms to change it." }
        : undefined;

  return (
    <AppShell profile={profile} page={page} onLogout={onLogout} heading={caseDetailHeading}>
      {error && (
        <Alert variant="destructive">
          <AlertDescription>Times are shown in UTC until the system timezone loads. {error}</AlertDescription>
        </Alert>
      )}
      {page === "overview" && <Overview profile={profile} notice={notice} />}
      {page === "cases" &&
        (caseId === null ? (
          <CasesPage permissions={profile.permissions} />
        ) : (
          <CaseDetailPage caseId={caseId} permissions={profile.permissions} />
        ))}
      {page === "catalog" &&
        (entryId === null ? (
          <CatalogPage permissions={profile.permissions} />
        ) : (
          <CatalogEntryPage entryId={entryId} permissions={profile.permissions} />
        ))}
      {page === "users" && isAdmin && <UsersPage currentUserId={profile.id} />}
      {page === "privacy" && isAdmin && <PrivacyPage />}
      {page === "roles" && isAdmin && <RolesMatrix editable />}
      {page === "settings" && isAdmin && <SettingsPage />}
      {page === "account" && (
        <>
          <ProfileCard />
          <ChangePassword
            heading="Change your password"
            description="This ends all of your sessions. You will need to log in again."
            onChange={changePassword}
            onDone={onPasswordChanged}
          />
        </>
      )}
    </AppShell>
  );
}
