import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, Derivation, EvidenceItem, getEvidence, getRedaction, listDerivatives, redactEvidence } from "../../api";
import { EvidenceDetail } from "./EvidenceDetail";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return {
    ...actual,
    getEvidence: vi.fn(),
    listDerivatives: vi.fn(),
    getRedaction: vi.fn(),
    redactEvidence: vi.fn(),
  };
});

vi.mock("../../system-timezone", () => ({
  useSystemTimezone: () => ({ timezone: "UTC", effectiveFrom: null, error: null, setCurrent: () => {}, reload: () => {} }),
}));

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const get = vi.mocked(getEvidence);
const derivatives = vi.mocked(listDerivatives);
const redaction = vi.mocked(getRedaction);
const redact = vi.mocked(redactEvidence);

const ORIGINAL_ID = "11111111-1111-4111-8111-111111111111";
const COPY_ID = "22222222-2222-4222-8222-222222222222";

const ORIGINAL: EvidenceItem = {
  id: ORIGINAL_ID,
  case_id: "case-1",
  uploaded_by: "user-1",
  original_filename: "notes.txt",
  media_type: "text/plain",
  size_bytes: 74,
  sha256_plaintext: "a".repeat(64),
  status: "available",
  created_at: "2026-11-01T06:00:00Z",
  tlp_label: null,
};

const COPY: EvidenceItem = {
  ...ORIGINAL,
  id: COPY_ID,
  original_filename: "redacted-notes.txt",
  tlp_label: "AMBER",
};

const DERIVATION: Derivation = {
  redaction_id: "33333333-3333-4333-8333-333333333333",
  original_evidence_id: ORIGINAL_ID,
  derived: COPY,
  manifest: {
    method: "replace_lines",
    replacement: "[REDACTED]",
    source_sha256: "a".repeat(64),
    tlp_label: "AMBER",
    lines_total: 4,
    removed_lines: 1,
    removed: [{ from_line: 3, to_line: 3, lines: 1, reason: "contains an account number" }],
  },
  claim_assessments: [
    {
      review_layer_id: "44444444-4444-4444-8444-444444444444",
      round_id: "55555555-5555-4555-8555-555555555555",
      layer: "model",
      outcome: "insufficient_evidence",
      reason: "The claim cites the original.",
    },
  ],
};

beforeEach(() => {
  get.mockReset();
  derivatives.mockReset();
  redaction.mockReset();
  redact.mockReset();
  get.mockResolvedValue(ORIGINAL);
  derivatives.mockResolvedValue([COPY]);
});

describe("EvidenceDetail", () => {
  it("shows the TLP label of an original as none, and the copies the caller may see", async () => {
    render(<EvidenceDetail evidenceId={ORIGINAL_ID} canWrite={false} />);

    expect(await screen.findByText("none (original, no TLP rule)")).toBeInTheDocument();
    const row = (await screen.findByText("redacted-notes.txt")).closest("tr") as HTMLElement;
    expect(within(row).getByText("AMBER")).toBeInTheDocument();
  });

  it("says when no copy is visible", async () => {
    derivatives.mockResolvedValue([]);
    render(<EvidenceDetail evidenceId={ORIGINAL_ID} canWrite={false} />);

    expect(await screen.findByText("No redacted copy of this item is visible to your account.")).toBeInTheDocument();
  });

  it("hides the redact form from a caller without evidence:write", async () => {
    render(<EvidenceDetail evidenceId={ORIGINAL_ID} canWrite={false} />);

    await screen.findByText("redacted-notes.txt");
    expect(screen.queryByRole("form", { name: "Make a redacted copy" })).toBeNull();
  });

  it("offers the redact form to a writer for an available text/plain item", async () => {
    render(<EvidenceDetail evidenceId={ORIGINAL_ID} canWrite />);

    expect(await screen.findByRole("form", { name: "Make a redacted copy" })).toBeInTheDocument();
  });

  it("says why no redact form is shown for a non-text item", async () => {
    get.mockResolvedValue({ ...ORIGINAL, media_type: "application/pdf" });
    render(<EvidenceDetail evidenceId={ORIGINAL_ID} canWrite />);

    expect(await screen.findByText("Only an available text/plain item can be redacted.")).toBeInTheDocument();
    expect(screen.queryByRole("form", { name: "Make a redacted copy" })).toBeNull();
  });

  it("shows the manifest and the claim assessments of a chosen copy", async () => {
    const user = userEvent.setup();
    redaction.mockResolvedValue(DERIVATION);
    render(<EvidenceDetail evidenceId={ORIGINAL_ID} canWrite={false} />);

    await user.click(await screen.findByRole("button", { name: "Show redaction" }));

    expect(redaction).toHaveBeenCalledWith(COPY_ID);
    expect(await screen.findByText("contains an account number")).toBeInTheDocument();
    expect(screen.getByText("The claim cites the original.")).toBeInTheDocument();
  });

  it("shows the TLP-hidden item with the same text as a missing one", async () => {
    get.mockRejectedValue(new ApiError(404, "evidence not found"));
    render(<EvidenceDetail evidenceId={ORIGINAL_ID} canWrite={false} />);

    expect(await screen.findByText("No evidence with that ID is available to your account.")).toBeInTheDocument();
  });

  it("makes a copy with the lines and the label chosen, then shows its redaction and reloads the copies", async () => {
    const user = userEvent.setup();
    redact.mockResolvedValue(DERIVATION);
    redaction.mockResolvedValue(DERIVATION);
    render(<EvidenceDetail evidenceId={ORIGINAL_ID} canWrite />);

    const form = await screen.findByRole("form", { name: "Make a redacted copy" });
    await user.type(within(form).getByLabelText("From line"), "3");
    await user.type(within(form).getByLabelText("Reason for range 1"), "contains an account number");
    await user.selectOptions(within(form).getByLabelText("TLP label of the copy"), "GREEN");
    await user.click(within(form).getByRole("button", { name: "Make redacted copy" }));

    await waitFor(() =>
      expect(redact).toHaveBeenCalledWith(ORIGINAL_ID, {
        removed: [{ from_line: 3, to_line: 3, reason: "contains an account number" }],
        tlp_label: "GREEN",
      }),
    );
    await waitFor(() => expect(derivatives).toHaveBeenCalledTimes(2));
    // The new copy is the one whose redaction is shown.
    expect(redaction).toHaveBeenCalledWith(COPY_ID);
  });

  it("refuses a range whose last line is before its first, before any request", async () => {
    const user = userEvent.setup();
    render(<EvidenceDetail evidenceId={ORIGINAL_ID} canWrite />);

    const form = await screen.findByRole("form", { name: "Make a redacted copy" });
    await user.type(within(form).getByLabelText("From line"), "5");
    await user.type(within(form).getByLabelText("To line"), "2");
    await user.type(within(form).getByLabelText("Reason for range 1"), "reason");
    await user.click(within(form).getByRole("button", { name: "Make redacted copy" }));

    expect(within(form).getByText("Range 1: enter line numbers from 1, with the first not after the last.")).toBeInTheDocument();
    expect(redact).not.toHaveBeenCalled();
  });

  it("shows the server refusal text when the label is less restrictive than the original", async () => {
    const user = userEvent.setup();
    redact.mockRejectedValue(new ApiError(422, "the TLP label must be at least as restrictive as the original's label"));
    render(<EvidenceDetail evidenceId={ORIGINAL_ID} canWrite />);

    const form = await screen.findByRole("form", { name: "Make a redacted copy" });
    await user.type(within(form).getByLabelText("From line"), "1");
    await user.type(within(form).getByLabelText("Reason for range 1"), "reason");
    await user.selectOptions(within(form).getByLabelText("TLP label of the copy"), "CLEAR");
    await user.click(within(form).getByRole("button", { name: "Make redacted copy" }));

    expect(
      await within(form).findByText("The copy's TLP label must be at least as restrictive as the original's label."),
    ).toBeInTheDocument();
  });
});
