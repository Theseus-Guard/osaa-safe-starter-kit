import { useEffect, useState } from "react";
import { toast } from "sonner";
import {
  DisclosurePreview,
  DisclosureVersion,
  ExternalNotice,
  NoticeChannel,
  approveDisclosure,
  exportDisclosure,
  getDisclosurePreview,
  listDisclosures,
  saveBlob,
  listNotices,
  recordNotice,
} from "../../api";
import { describeError } from "../../messages";
import { useSystemTimezone } from "../../system-timezone";
import { zonedParts, zonedToUtcIso } from "../../time";
import { humanize } from "./case-format";
import { DisabledHint } from "@/components/disabled-hint";
import { Timestamp } from "@/components/timestamp";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldDescription, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Textarea } from "@/components/ui/textarea";

interface Props {
  caseId: string;
  // The case is closed. Packages cannot change, so the approval is not offered.
  closed: boolean;
  // The caller has disclosures:approve. The backend also requires the case to be visible to the caller.
  canApprove: boolean;
  // The caller has disclosures:approve or disclosures:prepare. Without disclosures:approve the notice is recorded on the
  // manual path, which needs a reason.
  canRecordNotices: boolean;
}

const DIGEST_PATTERN = /^[0-9a-f]{64}$/;
// An identifier of the recipient kept outside SAFE. The same pattern the API uses.
const RECIPIENT_REF_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._:-]{0,63}$/;
const REASON_LIMIT = 500;
const CHANNEL_OTHER_LIMIT = 200;

const CHANNEL_TEXT: Record<Exclude<NoticeChannel, "other">, string> = {
  email: "Email",
  postal: "Postal",
  webhook: "Webhook",
  secure_manual: "Secure manual",
};

// A notice without a receipt is never shown as delivered. "Sent" means the send is recorded and no receipt is recorded yet.
const NOTICE_STATE_TEXT: Record<ExternalNotice["state"], string> = {
  sent: "Sent, no receipt recorded yet",
  receipt_received: "Receipt received",
  receipt_unknown: "Receipt unknown",
  follow_up_required: "Follow-up required",
  follow_up_done: "Follow-up done",
  cancelled: "Cancelled",
};

const APPROVAL_TEXT: Record<DisclosureVersion["approval_state"], string> = {
  approved: "Approved",
  not_approved: "Not approved",
  revoked_by_change: "Revoked by a later version",
};

// The Disclosure tab. It lists the package versions of the case and previews the chosen one. It shows the structure and
// the derivatives the caller may see; no evidence content is ever loaded. An approver can approve the latest version by
// typing its recipient, purpose and digest exactly as the preview shows them.
export function CaseDisclosures({ caseId, closed, canApprove, canRecordNotices }: Props) {
  const { timezone } = useSystemTimezone();
  const zone = timezone ?? "UTC";
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
  // The external notices of the chosen version, and the form that records a new one. Bumped after a notice is recorded.
  const [notices, setNotices] = useState<ExternalNotice[] | null>(null);
  const [noticesError, setNoticesError] = useState<string | null>(null);
  const [noticeRevision, setNoticeRevision] = useState(0);
  const [initialSent] = useState(() => zonedParts(new Date(), zone));
  const [noticeChannel, setNoticeChannel] = useState<NoticeChannel>("email");
  const [noticeChannelOther, setNoticeChannelOther] = useState("");
  const [noticeRecipient, setNoticeRecipient] = useState("");
  const [sentDate, setSentDate] = useState(initialSent.date);
  const [sentTime, setSentTime] = useState(initialSent.time);
  const [fallbackReason, setFallbackReason] = useState("");
  const [repeatReason, setRepeatReason] = useState("");
  const [noticeFormError, setNoticeFormError] = useState<string | null>(null);
  const [recording, setRecording] = useState(false);

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

  useEffect(() => {
    let stale = false;
    setNotices(null);
    setNoticesError(null);
    if (selectedId === null) return;
    listNotices(caseId, selectedId)
      .then((result) => {
        if (!stale) setNotices(result.items);
      })
      .catch((caught: unknown) => {
        if (!stale) setNoticesError(describeError(caught));
      });
    return () => {
      stale = true;
    };
  }, [caseId, selectedId, revision, noticeRevision]);

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

  // A notice is recorded against the latest approved version only. The approver records it on the normal path; anyone
  // else who may prepare packages records it on the manual path, with a reason.
  const noticeAllowed = selected !== null && selected.is_latest && selected.approval_state === "approved" && !closed;
  const noticeReason = closed
    ? "The case is closed. Its notices cannot change."
    : selected !== null && !selected.is_latest
      ? "A newer version of this package exists. Notices are recorded against the latest approved version."
      : selected !== null && selected.approval_state !== "approved"
        ? "Approve this version before a notice can be recorded for it."
        : undefined;

  async function recordSend() {
    if (selectedId === null) return;
    const recipient = noticeRecipient.trim();
    if (!RECIPIENT_REF_PATTERN.test(recipient)) {
      setNoticeFormError(
        "The recipient reference is an identifier: letters, digits, '.', '_', ':' or '-', up to 64 characters. Do not enter a name or an address.",
      );
      return;
    }
    const other = noticeChannelOther.trim();
    if (noticeChannel === "other" && (other === "" || other.length > CHANNEL_OTHER_LIMIT)) {
      setNoticeFormError(`Describe the channel, up to ${CHANNEL_OTHER_LIMIT} characters.`);
      return;
    }
    const reasonText = fallbackReason.trim();
    if (!canApprove && (reasonText === "" || reasonText.length > REASON_LIMIT)) {
      setNoticeFormError(
        `The approver is not recording this notice, so give the reason, up to ${REASON_LIMIT} characters. Deadlines are not paused.`,
      );
      return;
    }
    const repeatText = repeatReason.trim();
    if (repeatText.length > REASON_LIMIT) {
      setNoticeFormError(`The repeat reason can be at most ${REASON_LIMIT} characters.`);
      return;
    }
    const sentAt = zonedToUtcIso(sentDate, sentTime, zone);
    if (sentAt === null) {
      setNoticeFormError(
        `This date and time does not exist in ${zone} because of a daylight saving change. Choose another time.`,
      );
      return;
    }
    if (new Date(sentAt).getTime() > Date.now()) {
      setNoticeFormError("The time cannot be in the future. Enter the time the notice was sent.");
      return;
    }
    setNoticeFormError(null);
    setRecording(true);
    try {
      await recordNotice(caseId, selectedId, {
        channel: noticeChannel,
        channel_other: noticeChannel === "other" ? other : null,
        recipient_ref: recipient,
        sent_at: sentAt,
        manual_fallback: !canApprove,
        fallback_reason: canApprove ? null : reasonText,
        repeat_reason: repeatText === "" ? null : repeatText,
      });
      toast.success("Notice recorded as sent. No receipt is assumed until a receipt is recorded.");
      setNoticeChannelOther("");
      setNoticeRecipient("");
      setFallbackReason("");
      setRepeatReason("");
      setNoticeRevision((count) => count + 1);
    } catch (caught) {
      toast.error(describeError(caught));
    } finally {
      setRecording(false);
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

        {selected !== null && (
          <section aria-label="External notices" className="flex flex-col gap-3 border-t pt-4">
            <h3 className="text-sm font-medium">External notices of this version</h3>
            <p className="text-sm text-muted-foreground">
              A notice records a send that a person made. Sending is not done here, and nothing is sent again automatically.
              A notice with no receipt stays unknown.
            </p>
            {noticesError && (
              <Alert variant="destructive">
                <AlertDescription>{noticesError}</AlertDescription>
              </Alert>
            )}
            {notices === null && noticesError === null && (
              <p className="text-sm text-muted-foreground">Loading the notices…</p>
            )}
            {notices !== null && notices.length === 0 && (
              <p className="text-sm text-muted-foreground">No external notice is recorded for this version.</p>
            )}
            {notices !== null && notices.length > 0 && (
              <div className="overflow-hidden rounded-md border">
                <Table>
                  <TableHeader className="bg-muted">
                    <TableRow>
                      <TableHead>Sent</TableHead>
                      <TableHead>Channel</TableHead>
                      <TableHead>Recipient reference</TableHead>
                      <TableHead>State</TableHead>
                      <TableHead>Path</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {notices.map((notice) => (
                      <TableRow key={notice.id}>
                        <TableCell>
                          <Timestamp value={notice.sent_at} />
                        </TableCell>
                        <TableCell>
                          {notice.channel === "other" ? (notice.channel_other ?? "Other") : CHANNEL_TEXT[notice.channel]}
                        </TableCell>
                        <TableCell className="font-mono text-xs">{notice.recipient_ref}</TableCell>
                        <TableCell>{NOTICE_STATE_TEXT[notice.state]}</TableCell>
                        <TableCell>{notice.manual_fallback ? "Manual" : "Approver"}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </div>
            )}
            {canRecordNotices && (
              <form
                aria-label="Record a notice"
                className="flex flex-col gap-3"
                onSubmit={(event) => {
                  event.preventDefault();
                  void recordSend();
                }}
              >
                <h4 className="text-sm font-medium">Record a notice that was sent</h4>
                <Field>
                  <FieldLabel htmlFor="notice-channel">Channel</FieldLabel>
                  <Select
                    value={noticeChannel}
                    onValueChange={(value) => {
                      if (value) setNoticeChannel(value as NoticeChannel);
                    }}
                    disabled={!noticeAllowed}
                  >
                    <SelectTrigger id="notice-channel" className="w-full">
                      <SelectValue>
                        {(value: string | null) =>
                          value === "other" ? "Other" : value ? CHANNEL_TEXT[value as Exclude<NoticeChannel, "other">] : ""
                        }
                      </SelectValue>
                    </SelectTrigger>
                    <SelectContent>
                      {(Object.keys(CHANNEL_TEXT) as Exclude<NoticeChannel, "other">[]).map((channel) => (
                        <SelectItem key={channel} value={channel}>
                          {CHANNEL_TEXT[channel]}
                        </SelectItem>
                      ))}
                      <SelectItem value="other">Other</SelectItem>
                    </SelectContent>
                  </Select>
                </Field>
                {noticeChannel === "other" && (
                  <Field>
                    <FieldLabel htmlFor="notice-channel-other">Describe the channel</FieldLabel>
                    <Input
                      id="notice-channel-other"
                      value={noticeChannelOther}
                      onChange={(event) => setNoticeChannelOther(event.target.value)}
                      disabled={!noticeAllowed}
                    />
                  </Field>
                )}
                <Field>
                  <FieldLabel htmlFor="notice-recipient">Recipient reference</FieldLabel>
                  <Input
                    id="notice-recipient"
                    className="font-mono"
                    value={noticeRecipient}
                    onChange={(event) => setNoticeRecipient(event.target.value)}
                    disabled={!noticeAllowed}
                    aria-describedby="notice-recipient-help"
                  />
                  <FieldDescription id="notice-recipient-help">
                    An identifier kept outside SAFE, such as cert-07. Not a name or an address.
                  </FieldDescription>
                </Field>
                <Field>
                  <FieldLabel htmlFor="notice-sent-date">Sent on</FieldLabel>
                  <div className="grid grid-cols-2 gap-2">
                    <Input
                      id="notice-sent-date"
                      type="date"
                      value={sentDate}
                      onChange={(event) => setSentDate(event.target.value)}
                      disabled={!noticeAllowed}
                    />
                    <Input
                      id="notice-sent-time"
                      type="time"
                      aria-label="Time sent"
                      value={sentTime}
                      onChange={(event) => setSentTime(event.target.value)}
                      disabled={!noticeAllowed}
                    />
                  </div>
                  <FieldDescription>Times are in {zone}. The case stores the moment in UTC.</FieldDescription>
                </Field>
                {!canApprove && (
                  <Field>
                    <FieldLabel htmlFor="notice-fallback">Why the approver is not recording it</FieldLabel>
                    <Textarea
                      id="notice-fallback"
                      value={fallbackReason}
                      onChange={(event) => setFallbackReason(event.target.value)}
                      disabled={!noticeAllowed}
                      maxLength={REASON_LIMIT}
                    />
                    <FieldDescription>Manual path. Deadlines are not paused by recording a notice.</FieldDescription>
                  </Field>
                )}
                <Field>
                  <FieldLabel htmlFor="notice-repeat">Reason for a second notice (only if one exists)</FieldLabel>
                  <Input
                    id="notice-repeat"
                    value={repeatReason}
                    onChange={(event) => setRepeatReason(event.target.value)}
                    disabled={!noticeAllowed}
                  />
                </Field>
                {noticeFormError && (
                  <Alert variant="destructive">
                    <AlertDescription>{noticeFormError}</AlertDescription>
                  </Alert>
                )}
                <DisabledHint reason={noticeReason}>
                  <Button type="submit" disabled={!noticeAllowed || recording}>
                    Record notice
                  </Button>
                </DisabledHint>
              </form>
            )}
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
