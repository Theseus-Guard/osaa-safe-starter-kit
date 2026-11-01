import { ReactNode, useEffect, useState } from "react";
import {
  downloadPrivacyExport,
  getPrivacyInventory,
  PrivacyExportFormat,
  PrivacyInventory,
  PrivacyInventoryItem,
} from "../../api";
import { describeError } from "../../messages";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

// Amber marks a table whose retention period is not defined yet. The badge text states it too, so color is not the only cue.
const AMBER_BADGE = "border-transparent bg-amber-100 text-amber-900 dark:bg-amber-500/20 dark:text-amber-200";

export function PrivacyPage() {
  const [inventory, setInventory] = useState<PrivacyInventory | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [exportError, setExportError] = useState<string | null>(null);
  const [exporting, setExporting] = useState<PrivacyExportFormat | null>(null);

  useEffect(() => {
    let stale = false;
    getPrivacyInventory()
      .then((result) => {
        if (!stale) setInventory(result);
      })
      .catch((caught: unknown) => {
        if (!stale) setLoadError(describeError(caught));
      });
    return () => {
      stale = true;
    };
  }, []);

  async function handleExport(format: PrivacyExportFormat) {
    setExporting(format);
    setExportError(null);
    try {
      await downloadPrivacyExport(format);
    } catch (caught) {
      setExportError(describeError(caught));
    } finally {
      setExporting(null);
    }
  }

  const items = inventory?.items ?? [];
  const withPersonalData = items.filter((item) => item.contains_personal_data).length;
  const withRetention = items.filter((item) => item.retention_defined).length;

  return (
    <>
      <div className="flex flex-wrap gap-2">
        <Button variant="outline" disabled={inventory === null || exporting !== null} onClick={() => void handleExport("json")}>
          {exporting === "json" ? "Preparing JSON…" : "Export JSON"}
        </Button>
        <Button variant="outline" disabled={inventory === null || exporting !== null} onClick={() => void handleExport("csv")}>
          {exporting === "csv" ? "Preparing CSV…" : "Export CSV"}
        </Button>
      </div>

      {exportError && (
        <Alert variant="destructive">
          <AlertDescription>{exportError}</AlertDescription>
        </Alert>
      )}
      {loadError && (
        <Alert variant="destructive">
          <AlertDescription>{loadError}</AlertDescription>
        </Alert>
      )}
      {inventory === null && loadError === null && <p className="text-sm text-muted-foreground">Loading inventory…</p>}

      {inventory && (
        <>
          <Card>
            <CardHeader>
              <CardTitle>About this inventory</CardTitle>
              <CardDescription>Generated {formatTimestamp(inventory.generated_at)}.</CardDescription>
            </CardHeader>
            <CardContent>
              <dl className="grid gap-4 sm:grid-cols-3">
                <Detail label="What is stored">
                  {items.length} tables. {withPersonalData} contain personal data.
                </Detail>
                <Detail label="How it is protected">
                  Each table lists its security measures below. Secrets such as password hashes and tokens are never included.
                </Detail>
                <Detail label="How long">
                  Retention is defined for {withRetention} of {items.length} tables.
                </Detail>
              </dl>
            </CardContent>
          </Card>

          {items.length === 0 && <p className="text-sm text-muted-foreground">No tables are listed.</p>}
          {items.map((item) => (
            <InventoryCard key={item.table} item={item} />
          ))}
        </>
      )}
    </>
  );
}

function InventoryCard({ item }: { item: PrivacyInventoryItem }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle className="font-mono text-base">{item.table}</CardTitle>
        <CardDescription>{item.description}</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        <div className="flex flex-wrap gap-2">
          <Badge variant="secondary">{item.category}</Badge>
          {item.contains_personal_data ? <Badge>Contains personal data</Badge> : <Badge variant="outline">No personal data</Badge>}
          {!item.retention_defined && (
            <Badge variant="outline" className={AMBER_BADGE}>
              Retention not defined yet
            </Badge>
          )}
        </div>
        <dl className="grid gap-4 text-sm sm:grid-cols-2">
          <Detail label="Personal fields">{item.personal_fields.length > 0 ? item.personal_fields.join(", ") : "None"}</Detail>
          <Detail label="Purpose">{item.purpose}</Detail>
          <Detail label="Storage">{item.storage}</Detail>
          <Detail label="Retention">{item.retention}</Detail>
          <Detail label="Security">{item.security}</Detail>
          <Detail label="Deletion mode">{item.deletion_mode}</Detail>
          <Detail label="Rows">
            {item.rows.total} total, {item.rows.active} active, {item.rows.soft_deleted} soft-deleted
          </Detail>
          <Detail label="Oldest and newest record">
            {item.oldest_created_at === null
              ? "No rows"
              : `${formatTimestamp(item.oldest_created_at)} to ${formatTimestamp(item.newest_created_at)}`}
          </Detail>
        </dl>
      </CardContent>
    </Card>
  );
}

interface DetailProps {
  label: string;
  children: ReactNode;
}

function Detail({ label, children }: DetailProps) {
  return (
    <div className="grid gap-1">
      <dt className="font-medium">{label}</dt>
      <dd className="text-muted-foreground">{children}</dd>
    </div>
  );
}

function formatTimestamp(value: string | null): string {
  if (value === null) return "None";
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : date.toLocaleString("en-US", { dateStyle: "medium", timeStyle: "short" });
}
