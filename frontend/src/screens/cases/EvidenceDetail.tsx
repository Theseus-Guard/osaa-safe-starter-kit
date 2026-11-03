import { useEffect, useState } from "react";
import { Derivation, EvidenceItem, getEvidence, listDerivatives } from "../../api";
import { describeError } from "../../messages";
import { RedactForm } from "./RedactForm";
import { RedactionBlock } from "./RedactionBlock";
import { Timestamp } from "@/components/timestamp";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

interface Props {
  evidenceId: string;
  // evidence:write. Without it the redact form is not shown. The detail itself needs evidence:read, which the list checked.
  canWrite: boolean;
}

// Only text/plain items can be redacted (the server refuses the other media types).
const REDACTABLE_MEDIA_TYPE = "text/plain";

// One evidence item with its TLP label, the redacted copies of it that the caller may see, the manifest and claim
// assessments of a chosen copy, and the form to make a new copy. A TLP-hidden item shows the same "not available" text
// as a missing one.
export function EvidenceDetail({ evidenceId, canWrite }: Props) {
  const [item, setItem] = useState<EvidenceItem | null>(null);
  const [derivatives, setDerivatives] = useState<EvidenceItem[] | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Bumped after a new copy is stored so the derivative list reloads.
  const [revision, setRevision] = useState(0);

  useEffect(() => {
    let stale = false;
    setError(null);
    Promise.all([getEvidence(evidenceId), listDerivatives(evidenceId)])
      .then(([loaded, copies]) => {
        if (stale) return;
        setItem(loaded);
        setDerivatives(copies);
      })
      .catch((caught: unknown) => {
        if (!stale) setError(describeError(caught));
      });
    return () => {
      stale = true;
    };
  }, [evidenceId, revision]);

  function handleRedacted(derivation: Derivation) {
    setSelectedId(derivation.derived.id);
    setRevision((count) => count + 1);
  }

  if (error !== null) {
    return (
      <Alert variant="destructive">
        <AlertDescription>{error}</AlertDescription>
      </Alert>
    );
  }
  if (item === null || derivatives === null) {
    return <p className="text-sm text-muted-foreground">Loading the evidence…</p>;
  }

  const canRedact = canWrite && item.status === "available" && item.media_type === REDACTABLE_MEDIA_TYPE;

  return (
    <div className="flex flex-col gap-5 text-sm">
      <dl className="grid gap-2 sm:grid-cols-3">
        <div>
          <dt className="text-muted-foreground">File</dt>
          <dd className="break-all">{item.original_filename}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">TLP label</dt>
          <dd>
            {item.tlp_label ? <Badge variant="secondary">{item.tlp_label}</Badge> : "none (original, no TLP rule)"}
          </dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Uploaded</dt>
          <dd>
            <Timestamp value={item.created_at} />
          </dd>
        </div>
      </dl>

      <div className="flex flex-col gap-2">
        <h4 className="font-medium">Redacted copies you can see</h4>
        {derivatives.length === 0 ? (
          <p className="text-muted-foreground">No redacted copy of this item is visible to your account.</p>
        ) : (
          <div className="overflow-hidden rounded-md border">
            <Table>
              <TableHeader className="bg-muted">
                <TableRow>
                  <TableHead>File</TableHead>
                  <TableHead>TLP label</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {derivatives.map((copy) => (
                  <TableRow key={copy.id}>
                    <TableCell className="whitespace-normal">{copy.original_filename}</TableCell>
                    <TableCell>{copy.tlp_label ?? "none"}</TableCell>
                    <TableCell>{copy.status}</TableCell>
                    <TableCell>
                      <Button
                        size="sm"
                        variant="outline"
                        aria-pressed={selectedId === copy.id}
                        onClick={() => setSelectedId(selectedId === copy.id ? null : copy.id)}
                      >
                        {selectedId === copy.id ? "Hide redaction" : "Show redaction"}
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
        {selectedId !== null && <RedactionBlock key={selectedId} derivedId={selectedId} />}
      </div>

      {canRedact && (
        <div className="flex flex-col gap-2 border-t pt-4">
          <h4 className="font-medium">Make a redacted copy</h4>
          <RedactForm evidenceId={item.id} onRedacted={handleRedacted} />
        </div>
      )}
      {canWrite && !canRedact && (
        <p className="text-muted-foreground">Only an available text/plain item can be redacted.</p>
      )}
    </div>
  );
}
