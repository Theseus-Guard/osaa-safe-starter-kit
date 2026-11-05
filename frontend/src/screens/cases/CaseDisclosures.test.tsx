import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import {
  ApiError,
  DisclosurePreview,
  DisclosureVersion,
  ExternalNotice,
  NoticeState,
  approveDisclosure,
  cancelNotice,
  exportDisclosure,
  getDisclosurePreview,
  listDisclosures,
  listEscalations,
  recordNoticeFollowUp,
  recordNoticeReceipt,
  saveBlob,
  listNotices,
  recordNotice,
} from "../../api";
import { CaseDisclosures } from "./CaseDisclosures";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return {
    ...actual,
    listDisclosures: vi.fn(),
    getDisclosurePreview: vi.fn(),
    approveDisclosure: vi.fn(),
    exportDisclosure: vi.fn(),
    saveBlob: vi.fn(),
    listNotices: vi.fn(),
    listEscalations: vi.fn(),
    recordNotice: vi.fn(),
    recordNoticeReceipt: vi.fn(),
    recordNoticeFollowUp: vi.fn(),
    cancelNotice: vi.fn(),
  };
});

vi.mock("../../system-timezone", () => ({
  useSystemTimezone: () => ({ timezone: "UTC", effectiveFrom: null, error: null, setCurrent: () => {}, reload: () => {} }),
}));

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const list = vi.mocked(listDisclosures);
const preview = vi.mocked(getDisclosurePreview);
const approve = vi.mocked(approveDisclosure);
const exportOne = vi.mocked(exportDisclosure);
const save = vi.mocked(saveBlob);
const notices = vi.mocked(listNotices);
const escalations = vi.mocked(listEscalations);
const record = vi.mocked(recordNotice);
const receipt = vi.mocked(recordNoticeReceipt);
const followUp = vi.mocked(recordNoticeFollowUp);
const cancel = vi.mocked(cancelNotice);

const CASE_ID = "11111111-2222-4333-8444-555555555555";
const VERSION_ID = "aaaaaaaa-bbbb-4ccc-8ddd-000000000002";
const PACKAGE_ID = "aaaaaaaa-bbbb-4ccc-8ddd-000000000001";
const EVIDENCE_ID = "00000000-0000-4000-8000-000000000001";
const DIGEST = "a".repeat(64);

const VERSION: DisclosureVersion = {
  id: VERSION_ID,
  package_id: PACKAGE_ID,
  case_id: CASE_ID,
  version: 1,
  is_latest: true,
  template: "confidential_rapid_alert",
  tlp_audience: "AMBER",
  recipient: "Member CERT",
  purpose: "Rapid alert on the affected gateway",
  digest: DIGEST,
  approval_state: "not_approved",
  approval: null,
  evidence: [],
  withheld_count: 0,
  created_by: "bbbbbbbb-cccc-4ddd-8eee-000000000001",
  created_at: "2026-11-02T09:00:00Z",
};

const PREVIEW: DisclosurePreview = {
  version_id: VERSION_ID,
  package_id: PACKAGE_ID,
  case_id: CASE_ID,
  version: 1,
  template: {
    key: "confidential_rapid_alert",
    title: "Confidential rapid alert",
    sections: [
      { title: "Header", contains: "Case reference, recipient, purpose, and TLP audience" },
      { title: "Redaction manifest", contains: "Removed line ranges per item, with no removed text" },
    ],
  },
  recipient: "Member CERT",
  purpose: "Rapid alert on the affected gateway",
  tlp_audience: "AMBER",
  digest: DIGEST,
  approval_state: "not_approved",
  evidence: [
    {
      withheld: false,
      evidence_id: EVIDENCE_ID,
      media_type: "text/plain",
      size_bytes: 42,
      tlp_label: "AMBER",
      removed_lines: 1,
    },
    { withheld: true },
  ],
  withheld_count: 1,
  notice: "This preview shows the structure of the package.",
};

beforeEach(() => {
  list.mockReset();
  preview.mockReset();
  approve.mockReset();
  vi.mocked(toast.success).mockClear();
  vi.mocked(toast.error).mockClear();
  notices.mockReset();
  escalations.mockReset();
  record.mockReset();
  receipt.mockReset();
  followUp.mockReset();
  cancel.mockReset();
  list.mockResolvedValue({ items: [VERSION] });
  preview.mockResolvedValue(PREVIEW);
  notices.mockResolvedValue({ items: [] });
  escalations.mockResolvedValue({ items: [] });
});

const APPROVED: DisclosureVersion = {
  ...VERSION,
  approval_state: "approved",
  approval: {
    id: "cccccccc-dddd-4eee-8fff-000000000009",
    recipient: VERSION.recipient,
    recipient_ref: "cert-07",
    purpose: VERSION.purpose,
    digest: DIGEST,
    preparer_id: VERSION.created_by,
    approved_by: "dddddddd-eeee-4fff-8000-000000000001",
    approved_at: "2026-11-02T10:00:00Z",
    separation_rule: "preparer_differs_from_approver",
  },
};

const NOTICE_SENT: ExternalNotice = {
  id: "eeeeeeee-ffff-4000-8111-000000000001",
  revision: 1,
  case_id: CASE_ID,
  package_id: PACKAGE_ID,
  version_id: VERSION_ID,
  version: 1,
  channel: "email",
  channel_other: null,
  recipient_ref: "cert-07",
  sent_at: "2026-11-02T09:00:00Z",
  recorded_at: "2026-11-02T09:05:00Z",
  recorded_by: "dddddddd-eeee-4fff-8000-000000000001",
  manual_fallback: false,
  fallback_reason: null,
  repeat_reason: null,
  state: "sent",
  state_reason_code: null,
  state_reason: null,
  received_at: null,
  changed_by: "dddddddd-eeee-4fff-8000-000000000001",
  changed_at: "2026-11-02T09:05:00Z",
};

// Fills the record form with a valid recipient reference. The date and time default to the current moment, which is
// earlier than the submit, so the time check passes.
async function fillNotice(user: ReturnType<typeof userEvent.setup>, recipient: string) {
  await user.type(screen.getByLabelText("Recipient reference"), recipient);
}

describe("CaseDisclosures", () => {
  it("shows an empty state when the case has no package version", async () => {
    list.mockResolvedValue({ items: [] });
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove={false} canRecordNotices={false} />);

    expect(await screen.findByText("No disclosure package version exists for this case.")).toBeInTheDocument();
    expect(preview).not.toHaveBeenCalled();
  });

  it("lists the versions with their approval state and shows the preview of the newest one", async () => {
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove={false} canRecordNotices={false} />);

    const row = (await screen.findByText("Confidential rapid alert", { selector: "td" })).closest("tr") as HTMLElement;
    expect(within(row).getByText("Not approved")).toBeInTheDocument();
    expect(within(row).getByText("AMBER")).toBeInTheDocument();

    expect(await screen.findByText("Preview: Confidential rapid alert")).toBeInTheDocument();
    expect(preview).toHaveBeenCalledWith(CASE_ID, VERSION_ID);
    expect(screen.getByText(DIGEST)).toBeInTheDocument();
    expect(screen.getByText("Header:", { exact: false })).toBeInTheDocument();
    expect(screen.getByText(`${EVIDENCE_ID} · text/plain · 42 bytes · TLP AMBER · 1 line(s) removed`)).toBeInTheDocument();
    expect(screen.getByText("Withheld: outside your TLP audience.")).toBeInTheDocument();
  });

  it("marks an earlier version and offers no approval for it", async () => {
    list.mockResolvedValue({ items: [{ ...VERSION, is_latest: false, approval_state: "revoked_by_change" }] });
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove canRecordNotices={false} />);

    expect(await screen.findByText("Revoked by a later version", { selector: "td" })).toBeInTheDocument();
    expect(screen.getByText("1 (earlier)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve this version" })).toBeDisabled();
  });

  it("offers no approval form to a caller without disclosures:approve", async () => {
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove={false} canRecordNotices={false} />);

    await screen.findByText("Preview: Confidential rapid alert");
    expect(screen.queryByRole("form", { name: "Approve this version" })).not.toBeInTheDocument();
  });

  it("approves the version with the recipient, purpose and digest the approver typed, then reloads", async () => {
    const user = userEvent.setup();
    approve.mockResolvedValue({
      ...VERSION,
      approval_state: "approved",
      approval: {
        id: "cccccccc-dddd-4eee-8fff-000000000009",
        recipient: VERSION.recipient,
        purpose: VERSION.purpose,
        digest: DIGEST,
        preparer_id: VERSION.created_by,
        approved_by: "dddddddd-eeee-4fff-8000-000000000001",
        approved_at: "2026-11-02T10:00:00Z",
        separation_rule: "preparer_differs_from_approver",
      },
    });
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove canRecordNotices={false} />);
    await screen.findByText("Preview: Confidential rapid alert");

    await user.type(screen.getByLabelText("Recipient"), "  Member CERT  ");
    await user.type(screen.getByLabelText("Approved recipient reference"), "  cert-07 ");
    await user.type(screen.getByLabelText("Purpose"), "Rapid alert on the affected gateway");
    await user.type(screen.getByLabelText("Digest"), DIGEST);
    await user.click(screen.getByRole("button", { name: "Approve this version" }));

    await waitFor(() =>
      expect(approve).toHaveBeenCalledWith(CASE_ID, VERSION_ID, {
        recipient: "Member CERT",
        purpose: "Rapid alert on the affected gateway",
        digest: DIGEST,
        recipient_ref: "cert-07",
      }),
    );
    expect(toast.success).toHaveBeenCalledWith("Approval recorded. It covers this version and its digest only.");
    await waitFor(() => expect(list).toHaveBeenCalledTimes(2), { timeout: 3000 });
  });

  it("refuses a digest that is not 64 lower-case hex characters before any request is sent", async () => {
    const user = userEvent.setup();
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove canRecordNotices={false} />);
    await screen.findByText("Preview: Confidential rapid alert");

    await user.type(screen.getByLabelText("Recipient"), "Member CERT");
    await user.type(screen.getByLabelText("Approved recipient reference"), "cert-07");
    await user.type(screen.getByLabelText("Purpose"), "Rapid alert");
    await user.type(screen.getByLabelText("Digest"), "ABC");
    await user.click(screen.getByRole("button", { name: "Approve this version" }));

    expect(await screen.findByText(/64 lower-case hexadecimal characters/)).toBeInTheDocument();
    expect(approve).not.toHaveBeenCalled();
  });

  it("refuses an approved recipient reference that looks like a name before any request is sent", async () => {
    const user = userEvent.setup();
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove canRecordNotices={false} />);
    await screen.findByText("Preview: Confidential rapid alert");

    await user.type(screen.getByLabelText("Recipient"), "Member CERT");
    await user.type(screen.getByLabelText("Approved recipient reference"), "Member CERT");
    await user.type(screen.getByLabelText("Purpose"), "Rapid alert");
    await user.type(screen.getByLabelText("Digest"), DIGEST);
    await user.click(screen.getByRole("button", { name: "Approve this version" }));

    expect(await screen.findByText(/The recipient reference is an identifier/)).toBeInTheDocument();
    expect(approve).not.toHaveBeenCalled();
  });

  it("shows the refusal text for a binding mismatch and keeps the form", async () => {
    const user = userEvent.setup();
    approve.mockRejectedValue(new ApiError(409, "approval_binding_mismatch"));
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove canRecordNotices={false} />);
    await screen.findByText("Preview: Confidential rapid alert");

    await user.type(screen.getByLabelText("Recipient"), "Other CERT");
    await user.type(screen.getByLabelText("Approved recipient reference"), "cert-07");
    await user.type(screen.getByLabelText("Purpose"), "Rapid alert on the affected gateway");
    await user.type(screen.getByLabelText("Digest"), DIGEST);
    await user.click(screen.getByRole("button", { name: "Approve this version" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "The recipient, purpose or digest does not match this version. Copy them exactly as they are shown.",
      ),
      { timeout: 3000 },
    );
    expect(screen.getByLabelText("Recipient")).toHaveValue("Other CERT");
  });

  it("exports an approved version as a file named by its version number", async () => {
    const user = userEvent.setup();
    const approved: DisclosureVersion = {
      ...VERSION,
      approval_state: "approved",
      approval: {
        id: "cccccccc-dddd-4eee-8fff-000000000009",
        recipient: VERSION.recipient,
        recipient_ref: "cert-07",
        purpose: VERSION.purpose,
        digest: DIGEST,
        preparer_id: VERSION.created_by,
        approved_by: "dddddddd-eeee-4fff-8000-000000000001",
        approved_at: "2026-11-02T10:00:00Z",
        separation_rule: "preparer_differs_from_approver",
      },
    };
    list.mockResolvedValue({ items: [approved] });
    const blob = new Blob(["{}"], { type: "application/json" });
    exportOne.mockResolvedValue(blob);
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove={false} />);
    await screen.findByText("Preview: Confidential rapid alert");

    await user.click(screen.getByRole("button", { name: "Export JSON" }));

    await waitFor(() => expect(save).toHaveBeenCalledWith(blob, "disclosure-version-1.json"));
    expect(exportOne).toHaveBeenCalledWith(CASE_ID, VERSION_ID);
  });

  it("offers no export of a version that has no approval and says why", async () => {
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove={false} />);
    await screen.findByText("Preview: Confidential rapid alert");

    expect(screen.getByRole("button", { name: "Export JSON" })).toBeDisabled();
    expect(exportOne).not.toHaveBeenCalled();
  });

  it("shows the refusal when the export fails", async () => {
    const user = userEvent.setup();
    list.mockResolvedValue({
      items: [
        {
          ...VERSION,
          approval_state: "approved",
          approval: {
            id: "cccccccc-dddd-4eee-8fff-000000000009",
            recipient: VERSION.recipient,
            recipient_ref: "cert-07",
            purpose: VERSION.purpose,
            digest: DIGEST,
            preparer_id: VERSION.created_by,
            approved_by: "dddddddd-eeee-4fff-8000-000000000001",
            approved_at: "2026-11-02T10:00:00Z",
            separation_rule: "preparer_differs_from_approver",
          },
        },
      ],
    });
    exportOne.mockRejectedValue(new ApiError(409, "version_not_approved"));
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove={false} />);
    await screen.findByText("Preview: Confidential rapid alert");

    await user.click(screen.getByRole("button", { name: "Export JSON" }));

    await waitFor(() => expect(toast.error).toHaveBeenCalled());
    expect(save).not.toHaveBeenCalled();
  });

  it("locks the approval for a closed case and says why", async () => {
    render(<CaseDisclosures caseId={CASE_ID} closed canApprove canRecordNotices={false} />);
    await screen.findByText("Preview: Confidential rapid alert");

    expect(screen.getByRole("button", { name: "Approve this version" })).toBeDisabled();
    expect(screen.getByLabelText("Digest")).toBeDisabled();
  });

  it("shows a load error from the list", async () => {
    list.mockRejectedValue(new ApiError(404, "case not found"));
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove={false} canRecordNotices={false} />);

    expect(await screen.findByText("No case with that ID is available to your account.")).toBeInTheDocument();
  });

  it("lists the notices of the chosen version and never shows a notice without a receipt as delivered", async () => {
    notices.mockResolvedValue({
      items: [
        NOTICE_SENT,
        { ...NOTICE_SENT, id: "eeeeeeee-ffff-4000-8111-000000000002", state: "receipt_unknown", state_reason_code: "timeout", recipient_ref: "org.cert-08", channel: "other", channel_other: "Courier" },
      ],
    });
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove={false} canRecordNotices={false} />);

    const section = await screen.findByRole("region", { name: "External notices" });
    expect(await within(section).findByText("Sent, no receipt recorded yet")).toBeInTheDocument();
    expect(within(section).getByText("Receipt unknown")).toBeInTheDocument();
    expect(within(section).getByText("cert-07")).toBeInTheDocument();
    expect(within(section).getByText("Courier")).toBeInTheDocument();
    expect(within(section).queryByText("Receipt received")).not.toBeInTheDocument();
    expect(notices).toHaveBeenCalledWith(CASE_ID, VERSION_ID);
  });

  it("says when no notice is recorded for the version", async () => {
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove={false} canRecordNotices={false} />);

    expect(await screen.findByText("No external notice is recorded for this version.")).toBeInTheDocument();
  });

  it("shows the missing facts of the escalations of the chosen version", async () => {
    escalations.mockResolvedValue({
      items: [
        {
          id: "ffffffff-0000-4111-8222-000000000001",
          version_id: VERSION_ID,
          missing_facts: "Recipient contact is unconfirmed.\nThe event time is not known.",
          escalated_at: "2026-11-02T11:00:00Z",
        },
      ],
    });
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove={false} canRecordNotices={false} />);

    const section = await screen.findByRole("region", { name: "Missing facts" });
    expect(await within(section).findByText(/Recipient contact is unconfirmed/)).toBeInTheDocument();
    expect(within(section).getByText(/The event time is not known/)).toBeInTheDocument();
    expect(escalations).toHaveBeenCalledWith(CASE_ID, VERSION_ID);
  });

  it("says when no escalation is recorded for the version", async () => {
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove={false} canRecordNotices={false} />);

    const section = await screen.findByRole("region", { name: "Missing facts" });
    expect(await within(section).findByText("No escalation is recorded for this version.")).toBeInTheDocument();
  });

  it("offers the record form only to a caller who may record notices", async () => {
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove={false} canRecordNotices={false} />);

    await screen.findByText("No external notice is recorded for this version.");
    expect(screen.queryByRole("form", { name: "Record a notice" })).not.toBeInTheDocument();
  });

  it("records a notice on the normal path for the approver, with the channel, the reference and the time", async () => {
    const user = userEvent.setup();
    list.mockResolvedValue({ items: [APPROVED] });
    record.mockResolvedValue(NOTICE_SENT);
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove canRecordNotices />);
    await screen.findByText("Preview: Confidential rapid alert");

    await fillNotice(user, "cert-07");
    await user.click(screen.getByRole("button", { name: "Record notice" }));

    await waitFor(() => expect(record).toHaveBeenCalledTimes(1));
    const [caseArg, versionArg, body] = record.mock.calls[0];
    expect(caseArg).toBe(CASE_ID);
    expect(versionArg).toBe(VERSION_ID);
    expect(body).toMatchObject({
      channel: "email",
      channel_other: null,
      recipient_ref: "cert-07",
      manual_fallback: false,
      fallback_reason: null,
      repeat_reason: null,
    });
    expect(Number.isNaN(Date.parse(body.sent_at))).toBe(false);
    expect(toast.success).toHaveBeenCalledWith(
      "Notice recorded as sent. No receipt is assumed until a receipt is recorded.",
    );
  });

  it("asks a preparer who is not the approver for a reason on the manual path before any request", async () => {
    const user = userEvent.setup();
    list.mockResolvedValue({ items: [APPROVED] });
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove={false} canRecordNotices />);
    await screen.findByText("Preview: Confidential rapid alert");

    await fillNotice(user, "cert-07");
    await user.click(screen.getByRole("button", { name: "Record notice" }));

    expect(await screen.findByText(/give the reason, up to 500 characters/)).toBeInTheDocument();
    expect(record).not.toHaveBeenCalled();

    await user.type(screen.getByLabelText("Why the approver is not recording it"), "Approver on leave today");
    await user.click(screen.getByRole("button", { name: "Record notice" }));
    await waitFor(() => expect(record).toHaveBeenCalledTimes(1));
    expect(record.mock.calls[0][2]).toMatchObject({
      manual_fallback: true,
      fallback_reason: "Approver on leave today",
    });
  });

  it("refuses a recipient reference that looks like a name before any request is sent", async () => {
    const user = userEvent.setup();
    list.mockResolvedValue({ items: [APPROVED] });
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove canRecordNotices />);
    await screen.findByText("Preview: Confidential rapid alert");

    await fillNotice(user, "Jane Doe");
    await user.click(screen.getByRole("button", { name: "Record notice" }));

    expect(await screen.findByText(/Do not enter a name or an address/)).toBeInTheDocument();
    expect(record).not.toHaveBeenCalled();
  });

  it("shows the refusal text for a notice the API refuses and keeps the form", async () => {
    const user = userEvent.setup();
    list.mockResolvedValue({ items: [APPROVED] });
    record.mockRejectedValue(new ApiError(409, "version_superseded"));
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove canRecordNotices />);
    await screen.findByText("Preview: Confidential rapid alert");

    await fillNotice(user, "cert-07");
    await user.click(screen.getByRole("button", { name: "Record notice" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "A newer version of this package exists, so no notice can be recorded for this one. Use the latest approved version.",
      ),
    );
    expect(screen.getByLabelText("Recipient reference")).toHaveValue("cert-07");
  });

  it("locks the notice form for a version that is not approved yet", async () => {
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove canRecordNotices />);
    await screen.findByText("Preview: Confidential rapid alert");

    expect(screen.getByRole("button", { name: "Record notice" })).toBeDisabled();
    expect(screen.getByLabelText("Recipient reference")).toBeDisabled();
    expect(record).not.toHaveBeenCalled();
  });

  // A notice sent in the past, so a receipt dated in the test is never later than now.
  const NOTICE_OLD: ExternalNotice = { ...NOTICE_SENT, sent_at: "2020-01-01T09:00:00Z" };

  function noticeRow(recipient: string): HTMLElement {
    return screen.getByText(recipient, { selector: "td" }).closest("tr") as HTMLElement;
  }

  it.each<[NoticeState, string[]]>([
    ["sent", ["Record receipt", "Mark follow-up", "Cancel notice"]],
    ["receipt_unknown", ["Record receipt", "Mark follow-up", "Cancel notice"]],
    ["follow_up_required", ["Complete follow-up", "Cancel notice"]],
    ["follow_up_done", ["Mark follow-up", "Cancel notice"]],
    ["receipt_received", []],
    ["cancelled", []],
  ])("offers in state %s only the actions the server allows", async (state, expected) => {
    notices.mockResolvedValue({ items: [{ ...NOTICE_OLD, state }] });
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove canRecordNotices />);

    await screen.findByText("cert-07", { selector: "td" });
    const buttons = within(noticeRow("cert-07")).queryAllByRole("button");
    expect(buttons.map((button) => button.textContent)).toEqual(expected);
  });

  it("offers no notice action to a caller without disclosures:approve or disclosures:prepare, or on a closed case", async () => {
    notices.mockResolvedValue({ items: [NOTICE_OLD] });
    const { unmount } = render(
      <CaseDisclosures caseId={CASE_ID} closed={false} canApprove={false} canRecordNotices={false} />,
    );

    await screen.findByText("cert-07", { selector: "td" });
    expect(screen.queryByRole("columnheader", { name: "Actions" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Record receipt" })).not.toBeInTheDocument();
    unmount();

    render(<CaseDisclosures caseId={CASE_ID} closed canApprove canRecordNotices />);
    await screen.findByText("cert-07", { selector: "td" });
    expect(screen.queryByRole("button", { name: "Cancel notice" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Mark follow-up" })).not.toBeInTheDocument();
  });

  it("records a receipt that came in, with its time in UTC, and refreshes the list", async () => {
    const user = userEvent.setup();
    notices.mockResolvedValue({ items: [NOTICE_OLD] });
    receipt.mockResolvedValue({ ...NOTICE_OLD, state: "receipt_received" });
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove canRecordNotices />);

    await user.click(await screen.findByRole("button", { name: "Record receipt" }));
    const form = screen.getByRole("form", { name: "Record receipt" });
    fireEvent.change(within(form).getByLabelText("Receipt came in on"), { target: { value: "2020-01-02" } });
    fireEvent.change(within(form).getByLabelText("Time the receipt came in"), { target: { value: "10:30" } });
    await user.click(within(form).getByRole("button", { name: "Save receipt" }));

    await waitFor(() => expect(receipt).toHaveBeenCalledTimes(1));
    const [caseArg, versionArg, noticeArg, body] = receipt.mock.calls[0];
    expect([caseArg, versionArg, noticeArg]).toEqual([CASE_ID, VERSION_ID, NOTICE_OLD.id]);
    expect(body).toMatchObject({ outcome: "received", reason_code: null, reason: null });
    expect(new Date(String(body.received_at)).toISOString()).toBe("2020-01-02T10:30:00.000Z");
    expect(toast.success).toHaveBeenCalledWith("Receipt recorded. The notice is now marked as received.");
    await waitFor(() => expect(notices).toHaveBeenCalledTimes(2));
    expect(screen.queryByRole("form", { name: "Record receipt" })).not.toBeInTheDocument();
  });

  it("refuses a receipt dated in the future before any request is sent", async () => {
    const user = userEvent.setup();
    notices.mockResolvedValue({ items: [NOTICE_OLD] });
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove canRecordNotices />);

    await user.click(await screen.findByRole("button", { name: "Record receipt" }));
    const form = screen.getByRole("form", { name: "Record receipt" });
    fireEvent.change(within(form).getByLabelText("Receipt came in on"), { target: { value: "2999-01-01" } });
    await user.click(within(form).getByRole("button", { name: "Save receipt" }));

    expect(await within(form).findByText("The time cannot be in the future. Enter the time the receipt came in.")).toBeInTheDocument();
    expect(receipt).not.toHaveBeenCalled();
  });

  it("records an unknown receipt only with a reason code and its follow-up, and sends both", async () => {
    const user = userEvent.setup();
    notices.mockResolvedValue({ items: [NOTICE_OLD] });
    receipt.mockResolvedValue({ ...NOTICE_OLD, state: "receipt_unknown", state_reason_code: "no_answer" });
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove canRecordNotices />);

    await user.click(await screen.findByRole("button", { name: "Record receipt" }));
    const form = screen.getByRole("form", { name: "Record receipt" });
    await user.click(within(form).getByLabelText("Receipt"));
    await user.click(await screen.findByRole("option", { name: "No receipt known" }));
    await user.click(within(form).getByRole("button", { name: "Save receipt" }));

    expect(await within(form).findByText("Choose the reason why no receipt is known.")).toBeInTheDocument();
    expect(receipt).not.toHaveBeenCalled();

    await user.click(within(form).getByLabelText("Why no receipt is known"));
    await user.click(await screen.findByRole("option", { name: "The recipient did not answer" }));
    await user.click(within(form).getByRole("button", { name: "Save receipt" }));
    // The follow-up is required with an unknown receipt: it is recorded with it, in the same change.
    expect(
      await within(form).findByText("Enter the follow-up to chase the recipient. A notice with no receipt needs one."),
    ).toBeInTheDocument();
    expect(receipt).not.toHaveBeenCalled();

    await user.type(within(form).getByLabelText("Follow-up to chase the recipient"), "Call the member's security contact");
    await user.click(within(form).getByRole("button", { name: "Save receipt" }));

    await waitFor(() => expect(receipt).toHaveBeenCalledTimes(1));
    expect(receipt.mock.calls[0][2]).toBe(NOTICE_OLD.id);
    expect(receipt.mock.calls[0][3]).toEqual({
      outcome: "unknown",
      received_at: null,
      reason_code: "no_answer",
      reason: null,
      follow_up_reason: "Call the member's security contact",
    });
    expect(toast.success).toHaveBeenCalledWith("No receipt is known. A follow-up is recorded for this notice.");
  });

  it("marks a follow-up as required with its reason, and refuses an empty reason before any request", async () => {
    const user = userEvent.setup();
    notices.mockResolvedValue({ items: [NOTICE_OLD] });
    followUp.mockResolvedValue({ ...NOTICE_OLD, state: "follow_up_required" });
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove canRecordNotices />);

    await user.click(await screen.findByRole("button", { name: "Mark follow-up" }));
    const form = screen.getByRole("form", { name: "Mark follow-up" });
    await user.click(within(form).getByRole("button", { name: "Save follow-up" }));
    expect(await within(form).findByText("Enter the text. It is kept with the notice.")).toBeInTheDocument();
    expect(followUp).not.toHaveBeenCalled();

    await user.type(within(form).getByLabelText("Reason for the follow-up"), "Call the CERT on Monday");
    await user.click(within(form).getByRole("button", { name: "Save follow-up" }));

    await waitFor(() => expect(followUp).toHaveBeenCalledTimes(1));
    expect(followUp.mock.calls[0].slice(0, 3)).toEqual([CASE_ID, VERSION_ID, NOTICE_OLD.id]);
    expect(followUp.mock.calls[0][3]).toEqual({ action: "require", reason: "Call the CERT on Monday" });
    expect(toast.success).toHaveBeenCalledWith("Follow-up marked as required.");
  });

  it("completes a required follow-up with what was done", async () => {
    const user = userEvent.setup();
    notices.mockResolvedValue({ items: [{ ...NOTICE_OLD, state: "follow_up_required" }] });
    followUp.mockResolvedValue({ ...NOTICE_OLD, state: "follow_up_done" });
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove canRecordNotices />);

    await user.click(await screen.findByRole("button", { name: "Complete follow-up" }));
    const form = screen.getByRole("form", { name: "Complete follow-up" });
    await user.type(within(form).getByLabelText("What was done"), "Reached by phone");
    await user.click(within(form).getByRole("button", { name: "Mark follow-up done" }));

    await waitFor(() => expect(followUp).toHaveBeenCalledTimes(1));
    expect(followUp.mock.calls[0][3]).toEqual({ action: "complete", reason: "Reached by phone" });
    expect(toast.success).toHaveBeenCalledWith("Follow-up marked as done.");
  });

  it("cancels a notice with its reason, then refreshes the list", async () => {
    const user = userEvent.setup();
    notices.mockResolvedValue({ items: [NOTICE_OLD] });
    cancel.mockResolvedValue({ ...NOTICE_OLD, state: "cancelled" });
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove canRecordNotices />);

    await user.click(await screen.findByRole("button", { name: "Cancel notice" }));
    const form = screen.getByRole("form", { name: "Cancel notice" });
    await user.type(within(form).getByLabelText("Reason for cancelling"), "Sent to the wrong address");
    await user.click(within(form).getByRole("button", { name: "Confirm cancellation" }));

    await waitFor(() => expect(cancel).toHaveBeenCalledTimes(1));
    expect(cancel.mock.calls[0].slice(0, 3)).toEqual([CASE_ID, VERSION_ID, NOTICE_OLD.id]);
    expect(cancel.mock.calls[0][3]).toEqual({ reason: "Sent to the wrong address" });
    expect(toast.success).toHaveBeenCalledWith("Notice cancelled. It stays in the record with its reason.");
    await waitFor(() => expect(notices).toHaveBeenCalledTimes(2));
  });

  it("shows the server refusal for a notice action, keeps the form open and does not refresh the list", async () => {
    const user = userEvent.setup();
    notices.mockResolvedValue({ items: [NOTICE_OLD] });
    cancel.mockRejectedValue(new ApiError(409, "state_conflict"));
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove canRecordNotices />);

    await user.click(await screen.findByRole("button", { name: "Cancel notice" }));
    const form = screen.getByRole("form", { name: "Cancel notice" });
    await user.type(within(form).getByLabelText("Reason for cancelling"), "Sent twice");
    await user.click(within(form).getByRole("button", { name: "Confirm cancellation" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "The notice is no longer in a state that allows this change. Reload it and check its state.",
      ),
    );
    expect(screen.getByRole("form", { name: "Cancel notice" })).toBeInTheDocument();
    expect(within(form).getByLabelText("Reason for cancelling")).toHaveValue("Sent twice");
    expect(notices).toHaveBeenCalledTimes(1);
  });
});
