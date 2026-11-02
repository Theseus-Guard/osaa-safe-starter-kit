import { useEffect, useState } from "react";
import { Derivation, getRedaction } from "../../api";
import { describeError } from "../../messages";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

interface Props {
  // The redacted copy whose manifest and claim assessments are shown. An original has neither.
  derivedId: string;
}

// The manifest of a redacted copy: the removed line ranges and the reason for each. The removed text is never stored
// in the manifest, so it is never shown. The claim assessments say how each review claim that cites the original
// stands on the copy.
export function RedactionBlock({ derivedId }: Props) {
  const [derivation, setDerivation] = useState<Derivation | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let stale = false;
    setDerivation(null);
    setError(null);
    getRedaction(derivedId)
      .then((result) => {
        if (!stale) setDerivation(result);
      })
      .catch((caught: unknown) => {
        if (!stale) setError(describeError(caught));
      });
    return () => {
      stale = true;
    };
  }, [derivedId]);

  if (error !== null) {
    return (
      <Alert variant="destructive">
        <AlertDescription>{error}</AlertDescription>
      </Alert>
    );
  }
  if (derivation === null) return <p className="text-sm text-muted-foreground">Loading the redaction…</p>;

  const { manifest, claim_assessments: claims } = derivation;
  return (
    <div className="flex flex-col gap-4 text-sm">
      <dl className="grid gap-2 sm:grid-cols-3">
        <div>
          <dt className="text-muted-foreground">Original evidence</dt>
          <dd className="font-mono text-xs break-all">{derivation.original_evidence_id}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">TLP label of the copy</dt>
          <dd>{derivation.derived.tlp_label ?? "none"}</dd>
        </div>
        <div>
          <dt className="text-muted-foreground">Lines</dt>
          <dd>
            {manifest.removed_lines} removed of {manifest.lines_total}
          </dd>
        </div>
      </dl>

      <div>
        <h4 className="mb-2 font-medium">Removed ranges</h4>
        <div className="overflow-hidden rounded-md border">
          <Table>
            <TableHeader className="bg-muted">
              <TableRow>
                <TableHead>Lines</TableHead>
                <TableHead>Count</TableHead>
                <TableHead>Reason</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {manifest.removed.map((range) => (
                <TableRow key={`${range.from_line}-${range.to_line}`}>
                  <TableCell>
                    {range.from_line === range.to_line ? `${range.from_line}` : `${range.from_line}–${range.to_line}`}
                  </TableCell>
                  <TableCell>{range.lines}</TableCell>
                  <TableCell className="whitespace-normal">{range.reason}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      </div>

      <div>
        <h4 className="mb-2 font-medium">Claim assessments</h4>
        {claims.length === 0 ? (
          <p className="text-muted-foreground">No review claim cites the original, so none is assessed.</p>
        ) : (
          <div className="overflow-hidden rounded-md border">
            <Table>
              <TableHeader className="bg-muted">
                <TableRow>
                  <TableHead>Review layer</TableHead>
                  <TableHead>Outcome</TableHead>
                  <TableHead>Reason</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {claims.map((claim) => (
                  <TableRow key={claim.review_layer_id}>
                    <TableCell>{claim.layer}</TableCell>
                    <TableCell>{claim.outcome}</TableCell>
                    <TableCell className="whitespace-normal">{claim.reason}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </div>
    </div>
  );
}
