import { useEffect, useState } from "react";
import {
  CatalogEntry,
  CatalogEntryPage,
  Recommendation,
  RecommendationPage,
  listCatalogEntries,
  listRecommendations,
} from "../../api";
import { catalogAbilities } from "../../permissions";
import { describeError } from "../../messages";
import { hashFor } from "../../routes";
import { CatalogEntryForm, RecommendationForm } from "./CatalogAddForms";
import { Timestamp } from "@/components/timestamp";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

interface Props {
  // The caller's permission names, from /auth/me. They only hide controls; the backend checks every request.
  permissions: readonly string[];
}

// Every catalog user can read the list. Without catalog:read the page explains that and loads nothing.
export function CatalogPage({ permissions }: Props) {
  const abilities = catalogAbilities(permissions);
  if (!abilities.canRead) {
    return (
      <Alert>
        <AlertDescription>You do not have permission to view the catalog.</AlertDescription>
      </Alert>
    );
  }
  return <CatalogLists canWrite={abilities.canWrite} />;
}

function CatalogLists({ canWrite }: { canWrite: boolean }) {
  const [entries, setEntries] = useState<CatalogEntry[] | null>(null);
  const [entriesPage, setEntriesPage] = useState<CatalogEntryPage | null>(null);
  const [recommendations, setRecommendations] = useState<Recommendation[] | null>(null);
  const [recommendationsPage, setRecommendationsPage] = useState<RecommendationPage | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Bumped after a create so both lists reload from the first page.
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    let stale = false;
    setError(null);
    Promise.all([listCatalogEntries(), listRecommendations()])
      .then(([entryPage, recommendationPage]) => {
        if (stale) return;
        setEntriesPage(entryPage);
        setEntries(entryPage.items);
        setRecommendationsPage(recommendationPage);
        setRecommendations(recommendationPage.items);
      })
      .catch((caught: unknown) => {
        if (!stale) setError(describeError(caught));
      });
    return () => {
      stale = true;
    };
  }, [revision]);

  async function loadMoreEntries() {
    if (entriesPage?.next_cursor === null || entriesPage === null) return;
    try {
      const next = await listCatalogEntries(entriesPage.next_cursor);
      setEntriesPage(next);
      setEntries((current) => [...(current ?? []), ...next.items]);
    } catch (caught) {
      setError(describeError(caught));
    }
  }

  async function loadMoreRecommendations() {
    if (recommendationsPage?.next_cursor === null || recommendationsPage === null) return;
    try {
      const next = await listRecommendations(recommendationsPage.next_cursor);
      setRecommendationsPage(next);
      setRecommendations((current) => [...(current ?? []), ...next.items]);
    } catch (caught) {
      setError(describeError(caught));
    }
  }

  return (
    <div className="flex flex-col gap-6">
      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Entries</CardTitle>
          <CardDescription>
            The catalog of defences. An entry shows as verified only after a passing verification run. Withdrawn and
            superseded entries keep their history.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {entries === null && error === null && <p className="text-sm text-muted-foreground">Loading entries…</p>}
          {entries !== null && entries.length === 0 && <p className="text-sm text-muted-foreground">No catalog entries yet.</p>}
          {entries !== null && entries.length > 0 && (
            <div className="overflow-hidden rounded-md border">
              <Table>
                <TableHeader className="bg-muted">
                  <TableRow>
                    <TableHead>Title</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead>Version</TableHead>
                    <TableHead>Verified</TableHead>
                    <TableHead>Created</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {entries.map((entry) => (
                    <TableRow key={entry.id}>
                      <TableCell className="whitespace-normal">
                        <a className="font-medium underline-offset-4 hover:underline" href={hashFor("catalog", entry.id)}>
                          {entry.current.title}
                        </a>
                      </TableCell>
                      <TableCell>
                        <Badge variant="secondary">{entry.status}</Badge>
                      </TableCell>
                      <TableCell>{entry.version}</TableCell>
                      <TableCell>{entry.current.verified ? "Yes" : "No"}</TableCell>
                      <TableCell>
                        <Timestamp value={entry.created_at} />
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
          {entriesPage?.next_cursor && (
            <div>
              <Button size="sm" variant="outline" onClick={() => void loadMoreEntries()}>
                Load more entries
              </Button>
            </div>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Recommendations</CardTitle>
          <CardDescription>
            Suggested defences, oldest first. A recommendation has no result; a verification run records one.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          {recommendations === null && error === null && (
            <p className="text-sm text-muted-foreground">Loading recommendations…</p>
          )}
          {recommendations !== null && recommendations.length === 0 && (
            <p className="text-sm text-muted-foreground">No recommendations yet.</p>
          )}
          {recommendations !== null && recommendations.length > 0 && (
            <div className="overflow-hidden rounded-md border">
              <Table>
                <TableHeader className="bg-muted">
                  <TableRow>
                    <TableHead>Failure or system</TableHead>
                    <TableHead>Minimum control</TableHead>
                    <TableHead>Responsible</TableHead>
                    <TableHead>Due</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {recommendations.map((item) => (
                    <TableRow key={item.id}>
                      <TableCell className="whitespace-normal">{item.failure_or_system}</TableCell>
                      <TableCell className="whitespace-normal">{item.minimum_control}</TableCell>
                      <TableCell className="whitespace-normal">{item.responsible}</TableCell>
                      <TableCell>
                        <Timestamp value={item.due_at} />
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
          {recommendationsPage?.next_cursor && (
            <div>
              <Button size="sm" variant="outline" onClick={() => void loadMoreRecommendations()}>
                Load more recommendations
              </Button>
            </div>
          )}
        </CardContent>
      </Card>

      {canWrite && (
        <div className="grid gap-6 lg:grid-cols-2">
          <CatalogEntryForm onCreated={() => setRevision((count) => count + 1)} />
          <RecommendationForm onCreated={() => setRevision((count) => count + 1)} />
        </div>
      )}
    </div>
  );
}
