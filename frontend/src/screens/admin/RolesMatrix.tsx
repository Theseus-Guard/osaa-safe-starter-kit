import { useEffect, useState } from "react";
import { listRoles, RoleSummary } from "../../api";
import { describeError } from "../../messages";

export function RolesMatrix() {
  const [roles, setRoles] = useState<RoleSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let stale = false;
    listRoles()
      .then((result) => {
        if (!stale) setRoles(result);
      })
      .catch((caught: unknown) => {
        if (!stale) setError(describeError(caught));
      });
    return () => {
      stale = true;
    };
  }, []);

  return (
    <section className="card">
      <h2>Roles and permissions</h2>
      <p className="hint">Read-only. Roles and their permissions are defined by the server.</p>
      {error && <p className="error">{error}</p>}
      {roles && (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Role</th>
                <th>Permissions</th>
              </tr>
            </thead>
            <tbody>
              {roles.map((entry) => (
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
