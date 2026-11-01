import { FormEvent, useEffect, useState } from "react";
import { toast } from "sonner";
import {
  CustodyEvent,
  EvidenceItem,
  EvidencePage,
  downloadEvidence,
  listCustodyEvents,
  listEvidence,
  saveBlob,
  uploadEvidence,
} from "../../api";
import { describeError } from "../../messages";
import {
  ACCEPTED_EVIDENCE_TYPE_NAMES,
  ACCEPTED_EVIDENCE_TYPES,
  custodyActionLabel,
  describeDetails,
  evidenceStatusLabel,
  formatBytes,
} from "./case-format";
import { Timestamp } from "@/components/timestamp";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

const PAGE_SIZE = 20;

interface Props {
  caseId: string;
  // evidence:read. Without it the list, downloads and custody logs are not loaded or shown.
  canRead: boolean;
  // evidence:write. Without it the upload form is not shown.
  canWrite: boolean;
}

export function CaseEvidence({ caseId, canRead, canWrite }: Props) {
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<EvidencePage | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Bumped after an upload so the list reloads from the server.
  const [version, setVersion] = useState(0);
  const [openCustodyId, setOpenCustodyId] = useState<string | null>(null);
  const [custodyRevision, setCustodyRevision] = useState(0);
  const [downloadingId, setDownloadingId] = useState<string | null>(null);

  useEffect(() => {
    // Without evidence:read the server would answer 403, so the list is not requested at all.
    if (!canRead) return;
    let stale = false;
    setLoading(true);
    listEvidence(caseId, PAGE_SIZE, offset)
      .then((result) => {
        if (stale) return;
        setPage(result);
        setError(null);
      })
      .catch((caught: unknown) => {
        if (!stale) setError(describeError(caught));
      })
      .finally(() => {
        if (!stale) setLoading(false);
      });
    return () => {
      stale = true;
    };
  }, [caseId, offset, version, canRead]);

  function handleUploaded(item: EvidenceItem) {
    toast.success(`${item.original_filename} was uploaded.`);
    setOffset(0);
    setVersion((value) => value + 1);
  }

  async function download(item: EvidenceItem) {
    setDownloadingId(item.id);
    try {
      const blob = await downloadEvidence(item.id);
      saveBlob(blob, item.original_filename);
      // The download is now in the custody log. Bumping the revision remounts an open custody list so it reloads.
      setCustodyRevision((value) => value + 1);
    } catch (caught) {
      toast.error(describeError(caught));
    } finally {
      setDownloadingId(null);
    }
  }

  const shownFrom = page && page.items.length > 0 ? page.offset + 1 : 0;
  const shownTo = page ? page.offset + page.items.length : 0;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Evidence</CardTitle>
        <CardDescription>
          Files are encrypted at rest. Every upload, download and integrity check is recorded in the custody log.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {canWrite && <EvidenceUploadForm caseId={caseId} onUploaded={handleUploaded} />}

        {canRead && (
          <>
            {error && (
              <Alert variant="destructive">
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            )}
            {page && page.items.length === 0 && <p className="text-sm text-muted-foreground">No evidence uploaded yet.</p>}
            {page && page.items.length > 0 && (
              <div className="max-h-[50vh] overflow-auto rounded-md border [&_[data-slot=table-container]]:overflow-visible">
                <Table>
                  <TableHeader className="sticky top-0 z-10 bg-muted">
                    <TableRow>
                      <TableHead>File</TableHead>
                      <TableHead>Size</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead>SHA-256</TableHead>
                      <TableHead>Uploaded</TableHead>
                      <TableHead>Actions</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {page.items.map((item) => (
                      <EvidenceRowGroup
                        key={item.id}
                        item={item}
                        custodyOpen={openCustodyId === item.id}
                        custodyRevision={custodyRevision}
                        downloading={downloadingId === item.id}
                        onToggleCustody={() => setOpenCustodyId(openCustodyId === item.id ? null : item.id)}
                        onDownload={() => void download(item)}
                      />
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-sm text-muted-foreground" aria-live="polite">
                {loading ? "Loading…" : `Showing ${shownFrom}–${shownTo} of ${page?.total ?? 0}`}
              </p>
              <div className="flex gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  disabled={loading || !page || page.offset === 0}
                  onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                >
                  Previous
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  disabled={loading || !page || page.offset + page.limit >= page.total}
                  onClick={() => setOffset(offset + PAGE_SIZE)}
                >
                  Next
                </Button>
              </div>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  );
}

interface UploadProps {
  caseId: string;
  onUploaded: (item: EvidenceItem) => void;
}

function EvidenceUploadForm({ caseId, onUploaded }: UploadProps) {
  const [file, setFile] = useState<File | null>(null);
  const [inputKey, setInputKey] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (file === null) {
      setError("Choose a file to upload.");
      return;
    }
    setError(null);
    setBusy(true);
    try {
      const item = await uploadEvidence(caseId, file);
      setFile(null);
      // Remounting the input clears the chosen file name in the browser.
      setInputKey((value) => value + 1);
      onUploaded(item);
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-3 rounded-md border p-3">
      <Field>
        <FieldLabel htmlFor="evidence-file">Upload evidence</FieldLabel>
        <Input
          key={inputKey}
          id="evidence-file"
          type="file"
          accept={ACCEPTED_EVIDENCE_TYPES.join(",")}
          onChange={(event) => setFile(event.target.files?.[0] ?? null)}
        />
        <FieldDescription>Allowed types: {ACCEPTED_EVIDENCE_TYPE_NAMES}. The server also limits the file size.</FieldDescription>
      </Field>
      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      <div>
        <Button type="submit" size="sm" disabled={busy || file === null}>
          {busy ? "Uploading…" : "Upload"}
        </Button>
      </div>
    </form>
  );
}

interface RowProps {
  item: EvidenceItem;
  custodyOpen: boolean;
  custodyRevision: number;
  downloading: boolean;
  onToggleCustody: () => void;
  onDownload: () => void;
}

function EvidenceRowGroup({ item, custodyOpen, custodyRevision, downloading, onToggleCustody, onDownload }: RowProps) {
  // Only available evidence can be downloaded. The server answers 409 for any other status.
  const canDownload = item.status === "available";
  return (
    <>
      <TableRow>
        <TableCell className="whitespace-normal">
          <span className="font-medium">{item.original_filename}</span>
          <span className="block text-xs text-muted-foreground">{item.media_type}</span>
        </TableCell>
        <TableCell>{formatBytes(item.size_bytes)}</TableCell>
        <TableCell>
          <Badge variant="secondary">{evidenceStatusLabel(item.status)}</Badge>
        </TableCell>
        <TableCell className="font-mono text-xs" title={item.sha256_plaintext ?? undefined}>
          {item.sha256_plaintext ? `${item.sha256_plaintext.slice(0, 12)}…` : "Not available"}
        </TableCell>
        <TableCell>
          <Timestamp value={item.created_at} />
        </TableCell>
        <TableCell>
          <div className="flex flex-wrap gap-2">
            <Button size="sm" variant="outline" disabled={!canDownload || downloading} onClick={onDownload}>
              {downloading ? "Downloading…" : "Download"}
            </Button>
            <Button size="sm" variant="ghost" aria-expanded={custodyOpen} onClick={onToggleCustody}>
              {custodyOpen ? "Hide custody" : "Custody"}
            </Button>
          </div>
        </TableCell>
      </TableRow>
      {custodyOpen && (
        <TableRow>
          <TableCell colSpan={6} className="whitespace-normal bg-muted/40">
            <CustodyLog key={custodyRevision} evidenceId={item.id} />
          </TableCell>
        </TableRow>
      )}
    </>
  );
}

function CustodyLog({ evidenceId }: { evidenceId: string }) {
  const [events, setEvents] = useState<CustodyEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let stale = false;
    setEvents(null);
    setError(null);
    listCustodyEvents(evidenceId)
      .then((result) => {
        if (!stale) setEvents(result);
      })
      .catch((caught: unknown) => {
        if (!stale) setError(describeError(caught));
      });
    return () => {
      stale = true;
    };
  }, [evidenceId]);

  if (error) {
    return (
      <Alert variant="destructive">
        <AlertDescription>{error}</AlertDescription>
      </Alert>
    );
  }
  if (events === null) return <p className="text-sm text-muted-foreground">Loading custody log…</p>;
  if (events.length === 0) return <p className="text-sm text-muted-foreground">No custody events recorded.</p>;

  return (
    <ol className="flex flex-col gap-2 text-sm">
      {events.map((event) => (
        <li key={event.id} className="flex flex-col gap-0.5">
          <span className="font-medium">
            {custodyActionLabel(event.action)} <Timestamp value={event.created_at} />
          </span>
          <span className="font-mono text-xs text-muted-foreground">{event.actor_id ?? "System"}</span>
          {describeDetails(event.details).map((line) => (
            <span key={line} className="text-xs text-muted-foreground">
              {line}
            </span>
          ))}
        </li>
      ))}
    </ol>
  );
}
