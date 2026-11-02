import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import {
  ApiError,
  DisclosurePreview,
  DisclosureVersion,
  approveDisclosure,
  exportDisclosure,
  getDisclosurePreview,
  listDisclosures,
  saveBlob,
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
  list.mockResolvedValue({ items: [VERSION] });
  preview.mockResolvedValue(PREVIEW);
});

describe("CaseDisclosures", () => {
  it("shows an empty state when the case has no package version", async () => {
    list.mockResolvedValue({ items: [] });
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove={false} />);

    expect(await screen.findByText("No disclosure package version exists for this case.")).toBeInTheDocument();
    expect(preview).not.toHaveBeenCalled();
  });

  it("lists the versions with their approval state and shows the preview of the newest one", async () => {
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove={false} />);

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
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove />);

    expect(await screen.findByText("Revoked by a later version", { selector: "td" })).toBeInTheDocument();
    expect(screen.getByText("1 (earlier)")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve this version" })).toBeDisabled();
  });

  it("offers no approval form to a caller without disclosures:approve", async () => {
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove={false} />);

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
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove />);
    await screen.findByText("Preview: Confidential rapid alert");

    await user.type(screen.getByLabelText("Recipient"), "  Member CERT  ");
    await user.type(screen.getByLabelText("Purpose"), "Rapid alert on the affected gateway");
    await user.type(screen.getByLabelText("Digest"), DIGEST);
    await user.click(screen.getByRole("button", { name: "Approve this version" }));

    await waitFor(() =>
      expect(approve).toHaveBeenCalledWith(CASE_ID, VERSION_ID, {
        recipient: "Member CERT",
        purpose: "Rapid alert on the affected gateway",
        digest: DIGEST,
      }),
    );
    expect(toast.success).toHaveBeenCalledWith("Approval recorded. It covers this version and its digest only.");
    await waitFor(() => expect(list).toHaveBeenCalledTimes(2), { timeout: 3000 });
  });

  it("refuses a digest that is not 64 lower-case hex characters before any request is sent", async () => {
    const user = userEvent.setup();
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove />);
    await screen.findByText("Preview: Confidential rapid alert");

    await user.type(screen.getByLabelText("Recipient"), "Member CERT");
    await user.type(screen.getByLabelText("Purpose"), "Rapid alert");
    await user.type(screen.getByLabelText("Digest"), "ABC");
    await user.click(screen.getByRole("button", { name: "Approve this version" }));

    expect(await screen.findByText(/64 lower-case hexadecimal characters/)).toBeInTheDocument();
    expect(approve).not.toHaveBeenCalled();
  });

  it("shows the refusal text for a binding mismatch and keeps the form", async () => {
    const user = userEvent.setup();
    approve.mockRejectedValue(new ApiError(409, "approval_binding_mismatch"));
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove />);
    await screen.findByText("Preview: Confidential rapid alert");

    await user.type(screen.getByLabelText("Recipient"), "Other CERT");
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
    render(<CaseDisclosures caseId={CASE_ID} closed canApprove />);
    await screen.findByText("Preview: Confidential rapid alert");

    expect(screen.getByRole("button", { name: "Approve this version" })).toBeDisabled();
    expect(screen.getByLabelText("Digest")).toBeDisabled();
  });

  it("shows a load error from the list", async () => {
    list.mockRejectedValue(new ApiError(404, "case not found"));
    render(<CaseDisclosures caseId={CASE_ID} closed={false} canApprove={false} />);

    expect(await screen.findByText("No case with that ID is available to your account.")).toBeInTheDocument();
  });
});
