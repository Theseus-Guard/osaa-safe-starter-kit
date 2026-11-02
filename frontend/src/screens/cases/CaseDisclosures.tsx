import { useEffect, useState } from "react";
import { toast } from "sonner";
import {
  DisclosurePreview,
  DisclosureVersion,
  approveDisclosure,
  exportDisclosure,
  getDisclosurePreview,
  listDisclosures,
  saveBlob,
} from "../../api";
import { describeError } from "../../messages";
import { humanize } from "./case-format";
import { DisabledHint } from "@/components/disabled-hint";
import { Timestamp } from "@/components/timestamp";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

interface Props {
  caseId: string;
  // The case is closed. Packages cannot change, so the approval is not offered.
  closed: boolean;
  // The caller has disclosures:approve. The backend also requires the case to be visible to the caller.
  canApprove: boolean;
}

const DIGEST_PATTERN = /^[0-9a-f]{64}$/;

const APPROVAL_TEXT: Record<DisclosureVersion["approval_state"], string> = {
  approved: "Approved",
  not_approved: "Not approved",
  revoked_by_change: "Revoked by a later version",
};

// The Disclosure tab. It lists the package versions of the case and previews the chosen one. It shows the structure and
// the derivatives the caller may see; no evidence content is ever loaded. An approver can approve the latest version by
// typing its recipient, purpose and digest exactly as the preview shows them.
export function CaseDisclosures({ caseId, closed, canApprove }: Props) {
  const [versions, setVersions] = useState<DisclosureVersion[] | null>(null);
  const [listError, setListError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [preview, setPreview] = useState<DisclosurePreview | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  // Bumped after an approval so the list and the preview reload from the server.
  const [revision, setRevision] = useState(0);
  const [recipient, setRecipient] = useState("");
  const [purpose, setPurpose] = useState("");
  const [digest, setDigest] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [approving, setApproving] = useState(false);
  const [exporting, setExporting] = useState(false);

  useEffect(() => {
    let stale = false;
    setListError(null);
    listDisclosures(caseId)
      .then((result) => {
        if (stale) return;
        setVersions(result.items);
        // Keep the chosen version when it is still listed; otherwise show the newest one.
        setSelectedId((current) =>
          current !== null && result.items.some((item) => item.id === current) ? current : (result.items[0]?.id ?? null),
        );
      })
      .catch((caught: unknown) => {
        if (!stale) setListError(describeError(caught));
      });
    return () => {
      stale = true;
    };
  }, [caseId, revision]);

  useEffect(() => {
    let stale = false;
    setPreview(null);
    setPreviewError(null);
    if (selectedId === null) return;
    getDisclosurePreview(caseId, selectedId)
      .then((result) => {
        if (!stale) setPreview(result);
      })
      .catch((caught: unknown) => {
        if (!stale) setPreviewError(describeError(caught));
      });
    return () => {
      stale = true;
    };
  }, [caseId, selectedId, revision]);

  const selected = versions?.find((item) => item.id === selectedId) ?? null;
  // Only the latest version can be approved, and an approved version needs no second approval.
  const canApproveSelected =
    canApprove && !closed && selected !== null && selected.is_latest && selected.approval_state !== "approved";
  const approvalReason = closed
    ? "The case is closed. Its disclosure packages cannot change."
    : selected !== null && !selected.is_latest
      ? "A newer version of this package exists. Approve the latest version."
      : undefined;

  // Only an approved version can be exported. An earlier version that was approved is exported as history.
  const exportReason =
    selected !== null && selected.approval === null
      ? "Only an approved version can be exported. Approve it first."
      : undefined;

  async function exportSelected() {
    if (selected === null) return;
    setExporting(true);
    try {
      const blob = await exportDisclosure(caseId, selected.id);
      saveBlob(blob, `disclosure-version-${selected.version}.json`);
    } catch (caught) {
      toast.error(describeError(caught));
    } finally {
      setExporting(false);
    }
  }

  async function approve() {
    if (selectedId === null) return;
    const digestValue = digest.trim();
    if (!DIGEST_PATTERN.test(digestValue)) {
      setFormError("The digest is 64 lower-case hexadecimal characters. Copy it exactly from the preview.");
      return;
    }
    setFormError(null);
    setApproving(true);
    try {
      await approveDisclosure(caseId, selectedId, {
        recipient: recipient.trim(),
        purpose: purpose.trim(),
        digest: digestValue,
      });
      toast.success("Approval recorded. It covers this version and its digest only.");
      setRecipient("");
      setPurpose("");
      setDigest("");
      setRevision((count) => count + 1);
    } catch (caught) {
      toast.error(describeError(caught));
    } finally {
      setApproving(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Disclosure packages</CardTitle>
        <CardDescription>
          Redacted derivatives prepared for one recipient and one purpose. A version is never changed: a changed package is
          a new version. Nothing is sent from here.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {listError && (
          <Alert variant="destructive">
            <AlertDescription>{listError}</AlertDescription>
          </Alert>
        )}
        {versions === null && listError === null && (
          <p className="text-sm text-muted-foreground">Loading disclosure packages…</p>
        )}
        {versions !== null && versions.length === 0 && (
          <p className="text-sm text-muted-foreground">No disclosure package version exists for this case.</p>
        )}
        {versions !== null && versions.length > 0 && (
          <div className="overflow-hidden rounded-md border">
            <Table>
              <TableHeader className="bg-muted">
                <TableRow>
                  <TableHead>Version</TableHead>
                  <TableHead>Template</TableHead>
                  <TableHead>TLP audience</TableHead>
                  <TableHead>Approval</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead>Preview</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {versions.map((item) => (
                  <TableRow key={item.id} data-state={item.id === selectedId ? "selected" : undefined}>
                    <TableCell>
                      {item.version}
                      {item.is_latest ? "" : " (earlier)"}
                    </TableCell>
                    <TableCell>{humanize(item.template)}</TableCell>
                    <TableCell>{item.tlp_audience}</TableCell>
                    <TableCell>{APPROVAL_TEXT[item.approval_state]}</TableCell>
                    <TableCell>
                      <Timestamp value={item.created_at} />
                    </TableCell>
                    <TableCell>
                      <Button
                        size="sm"
                        variant="outline"
                        aria-pressed={item.id === selectedId}
                        onClick={() => setSelectedId(item.id)}
                      >
                        Preview
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}

        {previewError && (
          <Alert variant="destructive">
            <AlertDescription>{previewError}</AlertDescription>
          </Alert>
        )}
        {selectedId !== null && preview === null && previewError === null && (
          <p className="text-sm text-muted-foreground">Loading the preview…</p>
        )}
        {preview !== null && (
          <section aria-label="Preview" className="flex flex-col gap-3 border-t pt-4">
            <h3 className="text-sm font-medium">Preview: {preview.template.title}</h3>
            <dl className="grid gap-3 text-sm sm:grid-cols-2">
              <div className="grid gap-1">
                <dt className="font-medium">Recipient</dt>
                <dd className="text-muted-foreground">{preview.recipient}</dd>
              </div>
              <div className="grid gap-1">
                <dt className="font-medium">Purpose</dt>
                <dd className="text-muted-foreground">{preview.purpose}</dd>
              </div>
              <div className="grid gap-1">
                <dt className="font-medium">TLP audience</dt>
                <dd className="text-muted-foreground">{preview.tlp_audience}</dd>
              </div>
              <div className="grid gap-1">
                <dt className="font-medium">Digest</dt>
                <dd className="font-mono text-xs break-all text-muted-foreground">{preview.digest}</dd>
              </div>
            </dl>
            <div className="grid gap-1 text-sm">
              <h4 className="font-medium">Parts of the package</h4>
              <ul className="list-disc pl-5 text-muted-foreground">
                {preview.template.sections.map((section) => (
                  <li key={section.title}>
                    <span className="font-medium text-foreground">{section.title}:</span> {section.contains}
                  </li>
                ))}
              </ul>
            </div>
            <div className="grid gap-1 text-sm">
              <h4 className="font-medium">Evidence items (derivatives only)</h4>
              {preview.evidence.length === 0 && <p className="text-muted-foreground">No item.</p>}
              {preview.evidence.map((item, index) =>
                item.withheld ? (
                  <p key={`withheld-${index}`} className="text-muted-foreground">
                    Withheld: outside your TLP audience.
                  </p>
                ) : (
                  <p key={item.evidence_id} className="font-mono text-xs break-all text-muted-foreground">
                    {item.evidence_id} · {item.media_type} · {item.size_bytes} bytes · TLP {item.tlp_label} ·{" "}
                    {item.removed_lines ?? 0} line(s) removed
                  </p>
                ),
              )}
            </div>
            <p className="text-xs text-muted-foreground">{preview.notice}</p>
            <p className="text-sm">
              Approval: <span className="font-medium">{APPROVAL_TEXT[preview.approval_state]}</span>
            </p>
            <div className="flex flex-wrap items-center gap-3">
              <DisabledHint reason={exportReason}>
                <Button
                  size="sm"
                  variant="outline"
                  disabled={exportReason !== undefined || selected === null || exporting}
                  onClick={() => void exportSelected()}
                >
                  Export JSON
                </Button>
              </DisabledHint>
              {selected !== null && selected.approval !== null && !selected.is_latest && (
                <p className="text-xs text-muted-foreground">
                  An earlier approved version. Its export is marked as superseded history.
                </p>
              )}
            </div>
          </section>
        )}

        {canApprove && selected !== null && (
          <form
            aria-label="Approve this version"
            className="flex flex-col gap-3 border-t pt-4"
            onSubmit={(event) => {
              event.preventDefault();
              void approve();
            }}
          >
            <h3 className="text-sm font-medium">Approve this version</h3>
            <p className="text-sm text-muted-foreground">
              Type the recipient, purpose and digest exactly as the preview shows them. The approval covers this version only.
            </p>
            <Field>
              <FieldLabel htmlFor="disclosure-recipient">Recipient</FieldLabel>
              <Input
                id="disclosure-recipient"
                value={recipient}
                onChange={(event) => setRecipient(event.target.value)}
                disabled={!canApproveSelected}
              />
            </Field>
            <Field>
              <FieldLabel htmlFor="disclosure-purpose">Purpose</FieldLabel>
              <Input
                id="disclosure-purpose"
                value={purpose}
                onChange={(event) => setPurpose(event.target.value)}
                disabled={!canApproveSelected}
              />
            </Field>
            <Field>
              <FieldLabel htmlFor="disclosure-digest">Digest</FieldLabel>
              <Input
                id="disclosure-digest"
                className="font-mono"
                value={digest}
                aria-invalid={formError !== null}
                onChange={(event) => setDigest(event.target.value)}
                disabled={!canApproveSelected}
              />
            </Field>
            {formError && (
              <Alert variant="destructive">
                <AlertDescription>{formError}</AlertDescription>
              </Alert>
            )}
            <DisabledHint reason={approvalReason}>
              <Button type="submit" disabled={!canApproveSelected || approving}>
                Approve this version
              </Button>
            </DisabledHint>
          </form>
        )}
      </CardContent>
    </Card>
  );
}
