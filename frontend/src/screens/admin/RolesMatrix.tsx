import { useEffect, useState } from "react";
import { listPermissions, listRoles, PermissionSummary, Role, RoleSummary, updateRolePermissions } from "../../api";
import { describeError } from "../../messages";

// The backend refuses to save the admin role without these, so they stay checked and locked in the grid.
const ADMIN_REQUIRED = ["users:manage", "users:reset_password", "roles:manage"];
const REQUIRED_TITLE = "The admin role must keep this permission, so admins cannot lock themselves out.";

interface Props {
  editable: boolean;
}

interface Loaded {
  catalog: PermissionSummary[];
  roles: RoleSummary[];
}

export function RolesMatrix({ editable }: Props) {
  const [loaded, setLoaded] = useState<Loaded | null>(null);
  // Checkbox state per role name. It holds unsaved edits and is seeded from the server on every load.
  const [drafts, setDrafts] = useState<Record<string, Set<string>>>({});
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [savingRole, setSavingRole] = useState<Role | null>(null);
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    let stale = false;
    async function load(): Promise<Loaded> {
      const roles = await listRoles();
      // The permission catalog is only needed for the editable grid.
      const catalog = editable ? await listPermissions() : [];
      return { catalog, roles };
    }
    load()
      .then((result) => {
        if (stale) return;
        setLoaded(result);
        setLoadError(null);
        // Keep unsaved edits for roles the user is still working on. Seed the others from the server.
        setDrafts((previous) =>
          Object.fromEntries(result.roles.map((entry) => [entry.role, previous[entry.role] ?? new Set<string>(entry.permissions)])),
        );
      })
      .catch((caught: unknown) => {
        if (!stale) setLoadError(describeError(caught));
      });
    return () => {
      stale = true;
    };
  }, [editable, reloadToken]);

  // The permissions a role will have on save. The admin role always includes the required set.
  function selectionFor(role: Role): Set<string> {
    const selected = new Set<string>(drafts[role] ?? []);
    if (role === "admin") ADMIN_REQUIRED.forEach((name) => selected.add(name));
    return selected;
  }

  function isDirty(entry: RoleSummary): boolean {
    const saved = new Set<string>(entry.permissions);
    const selected = selectionFor(entry.role);
    return selected.size !== saved.size || [...selected].some((name) => !saved.has(name));
  }

  function toggle(role: Role, name: string, checked: boolean) {
    setDrafts((previous) => {
      const next = new Set<string>(previous[role]);
      if (checked) next.add(name);
      else next.delete(name);
      return { ...previous, [role]: next };
    });
  }

  async function save(entry: RoleSummary) {
    const permissions = [...selectionFor(entry.role)].sort();
    setSavingRole(entry.role);
    setActionError(null);
    setNotice(null);
    try {
      await updateRolePermissions(entry.role, permissions);
      setNotice(`Permissions for ${entry.role} saved.`);
      setReloadToken((value) => value + 1);
    } catch (caught) {
      setActionError(describeError(caught));
    } finally {
      setSavingRole(null);
    }
  }

  return (
    <section className="card">
      <h2>Roles and permissions</h2>
      {editable ? (
        <p className="hint">Changes apply immediately to everyone with that role.</p>
      ) : (
        <p className="hint">Read-only. Only admins can change role permissions.</p>
      )}
      {notice && <p className="notice">{notice}</p>}
      {actionError && <p className="error">{actionError}</p>}
      {loadError && <p className="error">{loadError}</p>}

      {loaded && editable && (
        <>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Role</th>
                  {loaded.catalog.map((permission) => (
                    <th key={permission.name} title={permission.description}>
                      {permission.name}
                    </th>
                  ))}
                  <th>Save</th>
                </tr>
              </thead>
              <tbody>
                {loaded.roles.map((entry) => {
                  const selected = selectionFor(entry.role);
                  const isAdmin = entry.role === "admin";
                  return (
                    <tr key={entry.role}>
                      <td>{entry.role}</td>
                      {loaded.catalog.map((permission) => {
                        const required = isAdmin && ADMIN_REQUIRED.includes(permission.name);
                        return (
                          <td key={permission.name} title={required ? REQUIRED_TITLE : undefined}>
                            <input
                              type="checkbox"
                              aria-label={`${entry.role} ${permission.name}`}
                              checked={selected.has(permission.name)}
                              disabled={required || savingRole !== null}
                              onChange={(e) => toggle(entry.role, permission.name, e.target.checked)}
                            />
                          </td>
                        );
                      })}
                      <td>
                        <button type="button" disabled={!isDirty(entry) || savingRole !== null} onClick={() => void save(entry)}>
                          {savingRole === entry.role ? "Saving…" : "Save"}
                        </button>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
          <dl className="legend">
            {loaded.catalog.map((permission) => (
              <div key={permission.name}>
                <dt>
                  <code>{permission.name}</code>
                </dt>
                <dd>{permission.description}</dd>
              </div>
            ))}
          </dl>
        </>
      )}

      {loaded && !editable && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Role</th>
                <th>Permissions</th>
              </tr>
            </thead>
            <tbody>
              {loaded.roles.map((entry) => (
                <tr key={entry.role}>
                  <td>{entry.role}</td>
                  <td>{entry.permissions.length > 0 ? entry.permissions.join(", ") : "None"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
