import { Fragment, useEffect, useState } from "react";
import { toast } from "sonner";
import { deleteUser, listUsers, Role, ROLES, updateUser, UserPage, UserSummary, UserUpdate } from "../../api";
import { describeError } from "../../messages";
import { DisabledHint } from "@/components/disabled-hint";
import { Timestamp } from "@/components/timestamp";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { EditProfileDialog } from "./EditProfileDialog";
import { ResetPasswordRow } from "./ResetPasswordRow";

const PAGE_SIZE = 20;
const COLUMN_COUNT = 7;
const PROTECTED_REASON = "The protected admin account cannot be changed.";
const SELF_RESET_REASON = "Use Change password on the Account page to change your own password.";
// A bounded height with a scroll container keeps the table header sticky.
const TABLE_FRAME_CLASS = "overflow-hidden rounded-md border";

interface Props {
  currentUserId: string;
  // Changes whenever data may be stale. Bumping it reloads the current page.
  revision: number;
  onChanged: () => void;
}

export function UserManagement({ currentUserId, revision, onChanged }: Props) {
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<UserPage | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [resettingId, setResettingId] = useState<string | null>(null);
  const [editing, setEditing] = useState<UserSummary | null>(null);

  useEffect(() => {
    let stale = false;
    setLoading(true);
    listUsers(PAGE_SIZE, offset)
      .then((result) => {
        if (stale) return;
        setPage(result);
        setLoadError(null);
      })
      .catch((caught: unknown) => {
        if (!stale) setLoadError(describeError(caught));
      })
      .finally(() => {
        if (!stale) setLoading(false);
      });
    return () => {
      stale = true;
    };
  }, [offset, revision]);

  function changePage(nextOffset: number) {
    setOffset(nextOffset);
    setResettingId(null);
    setActionError(null);
  }

  async function applyChange(user: UserSummary, changes: UserUpdate, success: string) {
    setBusy(true);
    setActionError(null);
    try {
      await updateUser(user.id, changes, user.revision);
      toast.success(success);
      onChanged();
    } catch (caught) {
      setActionError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  async function handleDelete(user: UserSummary) {
    const confirmed = window.confirm(
      `Delete ${user.username}? The user is deactivated and hidden from the list. The record is kept, not physically removed.`,
    );
    if (!confirmed) return;
    setBusy(true);
    setActionError(null);
    setResettingId(null);
    try {
      await deleteUser(user.id);
      // Deleting the last row of a page (not the first page) would leave an empty page, so step back one page.
      const isLastRowOnPage = page !== null && page.items.length === 1 && offset > 0;
      if (isLastRowOnPage) setOffset(Math.max(0, offset - PAGE_SIZE));
      toast.success(`${user.username} has been deleted.`);
      onChanged();
    } catch (caught) {
      setActionError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  function handlePasswordReset(username: string) {
    setResettingId(null);
    setActionError(null);
    toast.success(`Password reset for ${username}. All of their sessions have ended.`, {
      description: "Share the temporary password through a secure channel.",
    });
    onChanged();
  }

  const total = page?.total ?? 0;
  const shownFrom = page && page.items.length > 0 ? page.offset + 1 : 0;
  const shownTo = page ? page.offset + page.items.length : 0;

  return (
    <Card>
      <CardHeader>
        <CardTitle>User list</CardTitle>
        <CardAction>
          <Button variant="outline" size="sm" disabled={loading || busy} onClick={onChanged}>
            Refresh
          </Button>
        </CardAction>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {actionError && (
          <Alert variant="destructive">
            <AlertDescription>{actionError}</AlertDescription>
          </Alert>
        )}
        {loadError && (
          <Alert variant="destructive">
            <AlertDescription>{loadError}</AlertDescription>
          </Alert>
        )}
        <div className={TABLE_FRAME_CLASS}>
          <Table>
            <TableHeader className="sticky top-0 z-10 bg-muted">
              <TableRow>
                <TableHead>Username</TableHead>
                <TableHead>Email</TableHead>
                <TableHead>Role</TableHead>
                <TableHead>Status</TableHead>
                <TableHead>Must change password</TableHead>
                <TableHead>Created</TableHead>
                <TableHead>Actions</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {page?.items.map((user) => {
                const isSelf = user.id === currentUserId;
                // Protected accounts keep their role and status. Reset password stays available as the recovery path.
                const isLocked = isSelf || user.is_protected;
                const lockReason = (selfReason: string) => (isSelf ? selfReason : user.is_protected ? PROTECTED_REASON : undefined);
                return (
                  <Fragment key={user.id}>
                    <TableRow>
                      <TableCell>
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="font-medium">{user.username}</span>
                          {isSelf && <Badge variant="outline">You</Badge>}
                          {user.is_protected && <Badge variant="secondary">Protected</Badge>}
                        </div>
                      </TableCell>
                      <TableCell>
                        {user.email ? (
                          <span className="break-all">{user.email}</span>
                        ) : (
                          <Badge variant="outline" className="text-muted-foreground">
                            Missing
                          </Badge>
                        )}
                      </TableCell>
                      <TableCell>
                        <DisabledHint reason={isLocked ? lockReason("You cannot change your own role.") : undefined}>
                          <Select
                            value={user.role}
                            disabled={isLocked || busy}
                            onValueChange={(value) => {
                              if (value) void applyChange(user, { role: value as Role }, `${user.username} is now ${value}.`);
                            }}
                          >
                            <SelectTrigger size="sm" aria-label={`Role of ${user.username}`} className="w-36">
                              <SelectValue />
                            </SelectTrigger>
                            <SelectContent>
                              {ROLES.map((r) => (
                                <SelectItem key={r} value={r}>
                                  {r}
                                </SelectItem>
                              ))}
                            </SelectContent>
                          </Select>
                        </DisabledHint>
                      </TableCell>
                      <TableCell>
                        {user.is_active ? <Badge variant="secondary">Active</Badge> : <Badge variant="outline">Disabled</Badge>}
                      </TableCell>
                      <TableCell>{user.password_change_required ? "Yes" : "No"}</TableCell>
                      <TableCell>
                        <Timestamp value={user.created_at} />
                      </TableCell>
                      <TableCell>
                        <div className="flex flex-wrap gap-1.5">
                          <DisabledHint reason={isSelf ? "Edit your own profile on the Account page." : undefined}>
                            <Button variant="outline" size="sm" disabled={isSelf || busy} aria-label={`Edit ${user.username}`}
                              onClick={() => setEditing(user)}>
                              Edit
                            </Button>
                          </DisabledHint>
                          <DisabledHint reason={isLocked ? lockReason("You cannot change your own status.") : undefined}>
                            <Button
                              variant="outline"
                              size="sm"
                              aria-label={`${user.is_active ? "Disable" : "Enable"} ${user.username}`}
                              disabled={isLocked || busy}
                              onClick={() =>
                                void applyChange(
                                  user,
                                  { is_active: !user.is_active },
                                  `${user.username} has been ${user.is_active ? "disabled" : "enabled"}.`,
                                )
                              }
                            >
                              {user.is_active ? "Disable" : "Enable"}
                            </Button>
                          </DisabledHint>
                          <DisabledHint reason={isSelf ? SELF_RESET_REASON : undefined}>
                            <Button
                              variant="outline"
                              size="sm"
                              disabled={isSelf || busy}
                              aria-expanded={resettingId === user.id}
                              aria-label={`Reset password for ${user.username}`}
                              onClick={() => setResettingId(resettingId === user.id ? null : user.id)}
                            >
                              Reset password
                            </Button>
                          </DisabledHint>
                          <DisabledHint reason={isLocked ? lockReason("You cannot delete your own account.") : undefined}>
                            <Button variant="destructive" size="sm" disabled={isLocked || busy} aria-label={`Delete ${user.username}`} onClick={() => void handleDelete(user)}>
                              Delete
                            </Button>
                          </DisabledHint>
                        </div>
                      </TableCell>
                    </TableRow>
                    {resettingId === user.id && (
                      <ResetPasswordRow
                        userId={user.id}
                        username={user.username}
                        colSpan={COLUMN_COUNT}
                        onDone={handlePasswordReset}
                        onCancel={() => setResettingId(null)}
                      />
                    )}
                  </Fragment>
                );
              })}
            </TableBody>
          </Table>
        </div>
        {page && page.items.length === 0 && <p className="text-sm text-muted-foreground">No users found on this page.</p>}
        <div className="flex flex-wrap items-center justify-between gap-2">
          <p className="text-sm text-muted-foreground" aria-live="polite">
            {loading ? "Loading…" : `Showing ${shownFrom}–${shownTo} of ${total}`}
          </p>
          <div className="flex gap-2">
            <Button
              variant="outline"
              size="sm"
              disabled={loading || !page || page.offset === 0}
              onClick={() => changePage(Math.max(0, offset - PAGE_SIZE))}
            >
              Previous
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={loading || !page || page.offset + page.limit >= page.total}
              onClick={() => changePage(offset + PAGE_SIZE)}
            >
              Next
            </Button>
          </div>
        </div>
      </CardContent>
      <EditProfileDialog
        user={editing}
        onClose={() => setEditing(null)}
        onSaved={() => {
          setEditing(null);
          onChanged();
        }}
      />
    </Card>
  );
}
