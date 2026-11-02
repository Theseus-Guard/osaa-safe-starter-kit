import { useEffect, useState } from "react";
import { CatalogEntry, getCatalogEntry } from "../../api";
import { catalogAbilities } from "../../permissions";
import { describeError } from "../../messages";
import { CatalogContentForm, CatalogReasonForm, CatalogSupersedeForm, CatalogVerifyForm } from "./CatalogEntryForms";
import { Timestamp } from "@/components/timestamp";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

interface Props {
  entryId: string;
  // The caller's permission names. They only hide controls; the backend checks every request.
  permissions: readonly string[];
}

// Withdrawn and superseded entries accept no further change. The history stays visible.
export const CLOSED_CATALOG_STATUSES = ["withdrawn", "superseded"] as const;

export function CatalogEntryPage({ entryId, permissions }: Props) {
  const abilities = catalogAbilities(permissions);
  const [entry, setEntry] = useState<CatalogEntry | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Bumped after a change so the entry, with its history, reloads from the server.
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    if (!abilities.canRead) return;
    let stale = false;
    setError(null);
    getCatalogEntry(entryId)
      .then((result) => {
        if (!stale) setEntry(result);
      })
      .catch((caught: unknown) => {
        if (!stale) setError(describeError(caught));
      });
    return () => {
      stale = true;
    };
  }, [entryId, revision, abilities.canRead]);

  if (!abilities.canRead) {
    return (
      <Alert>
        <AlertDescription>You do not have permission to view the catalog.</AlertDescription>
      </Alert>
    );
  }

  const closed = entry !== null && (CLOSED_CATALOG_STATUSES as readonly string[]).includes(entry.status);

  return (
    <div className="flex flex-col gap-6">
      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      {entry === null && error === null && <p className="text-sm text-muted-foreground">Loading the catalog entry…</p>}
      {entry !== null && (
        <>
          <Card>
            <CardHeader>
              <CardTitle>{entry.current.title}</CardTitle>
              <CardDescription>
                Version {entry.version}, created <Timestamp value={entry.created_at} />
              </CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-4">
              <dl className="grid gap-2 text-sm sm:grid-cols-3">
                <div>
                  <dt className="text-muted-foreground">Status</dt>
                  <dd>
                    <Badge variant="secondary">{entry.status}</Badge>
                  </dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">Verified</dt>
                  <dd>{entry.current.verified ? "Yes" : "No"}</dd>
                </div>
                <div>
                  <dt className="text-muted-foreground">Last check</dt>
                  <dd>{entry.current.verification ? entry.current.verification.result : "None recorded"}</dd>
                </div>
              </dl>
              <div>
                <h3 className="text-sm font-medium">Body</h3>
                <p className="whitespace-pre-wrap text-sm">{entry.current.body}</p>
              </div>
              {entry.current.restricted && (
                <div className="rounded-md border p-3 text-sm">
                  <h3 className="font-medium">Restricted reference</h3>
                  <p className="mt-1 break-all font-mono text-xs">{entry.current.restricted.reference}</p>
                  <p className="mt-1 text-xs text-muted-foreground">Digest {entry.current.restricted.digest}</p>
                </div>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Version history</CardTitle>
              <CardDescription>Every version is kept. An earlier version never changes.</CardDescription>
            </CardHeader>
            <CardContent>
              <div className="overflow-hidden rounded-md border">
                <Table>
                  <TableHeader className="bg-muted">
                    <TableRow>
                      <TableHead>Version</TableHead>
                      <TableHead>Change</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead>Verified</TableHead>
                      <TableHead>Reason</TableHead>
                      <TableHead>Recorded</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {(entry.versions ?? []).map((version) => (
                      <TableRow key={version.version}>
                        <TableCell>{version.version}</TableCell>
                        <TableCell>{version.operation}</TableCell>
                        <TableCell>{version.status}</TableCell>
                        <TableCell>{version.verified ? "Yes" : "No"}</TableCell>
                        <TableCell className="whitespace-normal">{version.reason ?? "-"}</TableCell>
                        <TableCell>
                          <Timestamp value={version.created_at} />
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Verification history</CardTitle>
              <CardDescription>The verification runs attached to a version. Only a pass verifies the entry.</CardDescription>
            </CardHeader>
            <CardContent>
              {(entry.versions ?? []).filter((version) => version.verification !== null).length === 0 ? (
                <p className="text-sm text-muted-foreground">No verification run is attached to this entry yet.</p>
              ) : (
                <div className="overflow-hidden rounded-md border">
                  <Table>
                    <TableHeader className="bg-muted">
                      <TableRow>
                        <TableHead>Version</TableHead>
                        <TableHead>Subject</TableHead>
                        <TableHead>Method</TableHead>
                        <TableHead>Result</TableHead>
                        <TableHead>Checked</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {(entry.versions ?? [])
                        .filter((version) => version.verification !== null)
                        .map((version) => (
                          <TableRow key={version.version}>
                            <TableCell>{version.version}</TableCell>
                            <TableCell className="whitespace-normal">{version.verification?.subject}</TableCell>
                            <TableCell className="whitespace-normal">{version.verification?.method}</TableCell>
                            <TableCell>{version.verification?.result}</TableCell>
                            <TableCell>
                              {version.verification && <Timestamp value={version.verification.verified_at} />}
                            </TableCell>
                          </TableRow>
                        ))}
                    </TableBody>
                  </Table>
                </div>
              )}
            </CardContent>
          </Card>

          {abilities.canWrite &&
            (closed ? (
              <Alert>
                <AlertDescription>
                  This entry is {entry.status}. It accepts no further changes, and its history stays as it is.
                </AlertDescription>
              </Alert>
            ) : (
              <div className="grid gap-6 lg:grid-cols-2">
                <CatalogContentForm
                  key={`version-${entry.version}`}
                  entry={entry}
                  mode="version"
                  onSaved={() => setRevision((count) => count + 1)}
                />
                <CatalogContentForm
                  key={`correction-${entry.version}`}
                  entry={entry}
                  mode="correction"
                  onSaved={() => setRevision((count) => count + 1)}
                />
                <CatalogReasonForm
                  key={`dispute-${entry.version}`}
                  entry={entry}
                  action="dispute"
                  onSaved={() => setRevision((count) => count + 1)}
                />
                <CatalogReasonForm
                  key={`withdraw-${entry.version}`}
                  entry={entry}
                  action="withdraw"
                  onSaved={() => setRevision((count) => count + 1)}
                />
                <CatalogSupersedeForm
                  key={`supersede-${entry.version}`}
                  entry={entry}
                  onSaved={() => setRevision((count) => count + 1)}
                />
                <CatalogVerifyForm
                  key={`verify-${entry.version}`}
                  entry={entry}
                  onSaved={() => setRevision((count) => count + 1)}
                />
              </div>
            ))}
        </>
      )}
    </div>
  );
}
