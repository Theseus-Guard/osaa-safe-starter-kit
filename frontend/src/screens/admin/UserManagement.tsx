import { Fragment, useEffect, useState } from "react";
import { deleteUser, listUsers, Role, ROLES, updateUser, UserPage, UserSummary, UserUpdate } from "../../api";
import { describeError } from "../../messages";
import { ResetPasswordRow } from "./ResetPasswordRow";

const PAGE_SIZE = 20;
const COLUMN_COUNT = 6;
const PROTECTED_TITLE = "The protected admin account cannot be changed";

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
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [resettingId, setResettingId] = useState<string | null>(null);

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
    setNotice(null);
    setActionError(null);
  }

  async function applyChange(user: UserSummary, changes: UserUpdate, success: string) {
    setBusy(true);
    setActionError(null);
    setNotice(null);
    try {
      await updateUser(user.id, changes);
      setNotice(success);
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
    setNotice(null);
    setResettingId(null);
    try {
      await deleteUser(user.id);
      // Deleting the last row of a page (not the first page) would leave an empty page, so step back one page.
      const isLastRowOnPage = page !== null && page.items.length === 1 && offset > 0;
      if (isLastRowOnPage) setOffset(Math.max(0, offset - PAGE_SIZE));
      setNotice(`${user.username} has been deleted.`);
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
    setNotice(
      `Password reset for ${username}. All of their sessions have ended. Share the temporary password through a secure channel.`,
    );
    onChanged();
  }

  const total = page?.total ?? 0;
  const shownFrom = page && page.items.length > 0 ? page.offset + 1 : 0;
  const shownTo = page ? page.offset + page.items.length : 0;

  return (
    <section className="card">
      <h2>Users</h2>
      {notice && <p className="notice">{notice}</p>}
      {actionError && <p className="error">{actionError}</p>}
      {loadError && <p className="error">{loadError}</p>}
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Username</th>
              <th>Role</th>
              <th>Status</th>
              <th>Must change password</th>
              <th>Created</th>
              <th>Actions</th>
            </tr>
          </thead>
          <tbody>
            {page?.items.map((user) => {
              const isSelf = user.id === currentUserId;
              // Protected accounts keep their role and status. Reset password stays available as the recovery path.
              const isLocked = isSelf || user.is_protected;
              const lockTitle = (selfMessage: string) => (isSelf ? selfMessage : user.is_protected ? PROTECTED_TITLE : undefined);
              return (
                <Fragment key={user.id}>
                  <tr>
                    <td>
                      {user.username}
                      {isSelf && <span className="hint"> (you)</span>}
                      {user.is_protected && <span className="hint"> Protected</span>}
                    </td>
                    <td>
                      <select
                        aria-label={`Role of ${user.username}`}
                        value={user.role}
                        disabled={isLocked || busy}
                        title={lockTitle("You cannot change your own role")}
                        onChange={(e) => {
                          const role = e.target.value as Role;
                          void applyChange(user, { role }, `${user.username} is now ${role}.`);
                        }}
                      >
                        {ROLES.map((r) => (
                          <option key={r} value={r}>
                            {r}
                          </option>
                        ))}
                      </select>
                    </td>
                    <td>{user.is_active ? "Active" : "Disabled"}</td>
                    <td>{user.password_change_required ? "Yes" : "No"}</td>
                    <td>{formatDate(user.created_at)}</td>
                    <td>
                      <div className="actions">
                        <button
                          type="button"
                          className="secondary"
                          disabled={isLocked || busy}
                          title={lockTitle("You cannot change your own status")}
                          onClick={() =>
                            void applyChange(
                              user,
                              { is_active: !user.is_active },
                              `${user.username} has been ${user.is_active ? "disabled" : "enabled"}.`,
                            )
                          }
                        >
                          {user.is_active ? "Disable" : "Enable"}
                        </button>
                        <button
                          type="button"
                          className="secondary"
                          disabled={isSelf || busy}
                          title={isSelf ? "Use Change password to change your own password" : undefined}
                          onClick={() => setResettingId(resettingId === user.id ? null : user.id)}
                        >
                          Reset password
                        </button>
                        <button
                          type="button"
                          className="danger"
                          disabled={isLocked || busy}
                          title={lockTitle("You cannot delete your own account")}
                          onClick={() => void handleDelete(user)}
                        >
                          Delete
                        </button>
                      </div>
                    </td>
                  </tr>
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
          </tbody>
        </table>
      </div>
      {page && page.items.length === 0 && <p className="hint">No users found on this page.</p>}
      <div className="pager">
        <span className="hint">
          {loading ? "Loading…" : `Showing ${shownFrom}–${shownTo} of ${total}`}
        </span>
        <div className="row">
          <button
            type="button"
            className="secondary"
            disabled={loading || !page || page.offset === 0}
            onClick={() => changePage(Math.max(0, offset - PAGE_SIZE))}
          >
            Previous
          </button>
          <button
            type="button"
            className="secondary"
            disabled={loading || !page || page.offset + page.limit >= page.total}
            onClick={() => changePage(offset + PAGE_SIZE)}
          >
            Next
          </button>
          <button type="button" className="secondary" disabled={loading || busy} onClick={onChanged}>
            Refresh
          </button>
        </div>
      </div>
    </section>
  );
}

function formatDate(value: string): string {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleDateString("en-US", { year: "numeric", month: "short", day: "numeric" });
}
