import { useEffect, useState } from "react";
import { listUsers, Me } from "../api";
import { describeError } from "../messages";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

interface Props {
  profile: Me;
  notice: string | null;
}

export function Overview({ profile, notice }: Props) {
  const isAdmin = profile.role === "admin";
  const [userTotal, setUserTotal] = useState<number | null>(null);
  const [totalError, setTotalError] = useState<string | null>(null);

  useEffect(() => {
    if (!isAdmin) return;
    let stale = false;
    // Only the total is used here. A single-row page keeps the request small.
    listUsers(1, 0)
      .then((result) => {
        if (!stale) setUserTotal(result.total);
      })
      .catch((caught: unknown) => {
        if (!stale) setTotalError(describeError(caught));
      });
    return () => {
      stale = true;
    };
  }, [isAdmin]);

  return (
    <>
      {notice && (
        <Alert>
          <AlertDescription>{notice}</AlertDescription>
        </Alert>
      )}
      <Card>
        <CardHeader>
          <CardTitle>Welcome, {profile.username}</CardTitle>
          <CardDescription>
            You are signed in with the <Badge variant="secondary">{profile.role}</Badge> role.
          </CardDescription>
        </CardHeader>
      </Card>
      <div className="grid gap-4 sm:grid-cols-2">
        <StatCard
          label="Password change"
          value={profile.password_change_required ? "Required" : "Not required"}
          hint={profile.password_change_required ? "You must set a new password before you continue." : "No action needed."}
        />
        {isAdmin && (
          <StatCard
            label="Accounts"
            value={userTotal !== null ? String(userTotal) : totalError ? "Unavailable" : "Loading…"}
            hint="Accounts listed on the Users page."
          />
        )}
      </div>
      {totalError && (
        <Alert variant="destructive">
          <AlertDescription>{totalError}</AlertDescription>
        </Alert>
      )}
    </>
  );
}

interface StatCardProps {
  label: string;
  value: string;
  hint: string;
}

function StatCard({ label, value, hint }: StatCardProps) {
  return (
    <Card>
      <CardHeader>
        <CardDescription>{label}</CardDescription>
        <CardTitle className="text-2xl">{value}</CardTitle>
      </CardHeader>
      <CardContent>
        <p className="text-sm text-muted-foreground">{hint}</p>
      </CardContent>
    </Card>
  );
}
