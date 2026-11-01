import { useEffect, useState } from "react";
import { toast } from "sonner";
import { listPermissions, listRoles, PermissionSummary, Role, RoleSummary, updateRolePermissions } from "../../api";
import { describeError } from "../../messages";
import { DisabledHint } from "@/components/disabled-hint";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

// The backend refuses to save the admin role without these, so they stay checked and locked in the grid.
const ADMIN_REQUIRED = ["users:manage", "users:reset_password", "roles:manage"];
const REQUIRED_REASON = "The admin role must keep this permission, so admins cannot lock themselves out.";
// A bounded height with a scroll container keeps the header sticky.
const TABLE_FRAME_CLASS = "overflow-hidden rounded-md border";

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
    try {
      await updateRolePermissions(entry.role, permissions);
      toast.success(`Permissions for ${entry.role} saved.`);
      setReloadToken((value) => value + 1);
    } catch (caught) {
      setActionError(describeError(caught));
    } finally {
      setSavingRole(null);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Permissions by role</CardTitle>
        <CardDescription>
          {editable ? "Changes apply immediately to everyone with that role." : "Read-only. Only admins can change role permissions."}
        </CardDescription>
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

        {loaded && editable && (
          <>
            <div className={TABLE_FRAME_CLASS}>
              <Table>
                <TableHeader className="sticky top-0 z-10 bg-muted">
                  <TableRow>
                    <TableHead>Role</TableHead>
                    {loaded.catalog.map((permission) => (
                      <TableHead key={permission.name} title={permission.description} className="whitespace-normal">
                        <span className="font-mono text-xs">{permission.name}</span>
                      </TableHead>
                    ))}
                    <TableHead>Save</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {loaded.roles.map((entry) => {
                    const selected = selectionFor(entry.role);
                    const isAdmin = entry.role === "admin";
                    return (
                      <TableRow key={entry.role}>
                        <TableCell className="font-medium">{entry.role}</TableCell>
                        {loaded.catalog.map((permission) => {
                          const required = isAdmin && ADMIN_REQUIRED.includes(permission.name);
                          return (
                            <TableCell key={permission.name}>
                              <DisabledHint reason={required ? REQUIRED_REASON : undefined}>
                                <Checkbox
                                  aria-label={`${entry.role} ${permission.name}`}
                                  checked={selected.has(permission.name)}
                                  disabled={required || savingRole !== null}
                                  onCheckedChange={(checked) => toggle(entry.role, permission.name, checked)}
                                />
                              </DisabledHint>
                            </TableCell>
                          );
                        })}
                        <TableCell>
                          <Button
                            size="sm"
                            disabled={!isDirty(entry) || savingRole !== null}
                            onClick={() => void save(entry)}
                          >
                            {savingRole === entry.role ? "Saving…" : "Save"}
                          </Button>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </div>
            <div className="flex flex-col gap-3">
              <h3 className="text-sm font-medium">Permission descriptions</h3>
              <dl className="grid gap-3 text-sm sm:grid-cols-2">
                {loaded.catalog.map((permission) => (
                  <div key={permission.name} className="grid gap-0.5">
                    <dt className="font-mono text-xs font-medium">{permission.name}</dt>
                    <dd className="text-muted-foreground">{permission.description}</dd>
                  </div>
                ))}
              </dl>
            </div>
          </>
        )}

        {loaded && !editable && (
          <div className={TABLE_FRAME_CLASS}>
            <Table>
              <TableHeader className="sticky top-0 z-10 bg-muted">
                <TableRow>
                  <TableHead>Role</TableHead>
                  <TableHead>Permissions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {loaded.roles.map((entry) => (
                  <TableRow key={entry.role}>
                    <TableCell className="font-medium">{entry.role}</TableCell>
                    <TableCell className="whitespace-normal">
                      {entry.permissions.length > 0 ? entry.permissions.join(", ") : "None"}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
