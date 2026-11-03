import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, Derivation, getRedaction } from "../../api";
import { RedactionBlock } from "./RedactionBlock";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, getRedaction: vi.fn() };
});

vi.mock("../../system-timezone", () => ({
  useSystemTimezone: () => ({ timezone: "UTC", effectiveFrom: null, error: null, setCurrent: () => {}, reload: () => {} }),
}));

const redaction = vi.mocked(getRedaction);

const DERIVATION: Derivation = {
  redaction_id: "33333333-3333-4333-8333-333333333333",
  original_evidence_id: "11111111-1111-4111-8111-111111111111",
  derived: {
    id: "22222222-2222-4222-8222-222222222222",
    case_id: "case-1",
    uploaded_by: "user-1",
    original_filename: "redacted-notes.txt",
    media_type: "text/plain",
    size_bytes: 60,
    sha256_plaintext: "b".repeat(64),
    status: "available",
    created_at: "2026-11-01T06:00:00Z",
    tlp_label: "GREEN",
  },
  manifest: {
    method: "replace_lines",
    replacement: "[REDACTED]",
    source_sha256: "a".repeat(64),
    tlp_label: "GREEN",
    lines_total: 10,
    removed_lines: 4,
    removed: [
      { from_line: 2, to_line: 2, lines: 1, reason: "customer name" },
      { from_line: 5, to_line: 8, lines: 4, reason: "account details" },
    ],
  },
  claim_assessments: [],
};

beforeEach(() => {
  redaction.mockReset();
});

describe("RedactionBlock", () => {
  it("shows each removed range with its count and reason, and the line totals", async () => {
    redaction.mockResolvedValue(DERIVATION);
    render(<RedactionBlock derivedId={DERIVATION.derived.id} />);

    expect(await screen.findByText("customer name")).toBeInTheDocument();
    expect(screen.getByText("account details")).toBeInTheDocument();
    expect(screen.getByText("5–8")).toBeInTheDocument();
    expect(screen.getByText("4 removed of 10")).toBeInTheDocument();
  });

  it("says when no review claim cites the original", async () => {
    redaction.mockResolvedValue(DERIVATION);
    render(<RedactionBlock derivedId={DERIVATION.derived.id} />);

    expect(await screen.findByText("No review claim cites the original, so none is assessed.")).toBeInTheDocument();
  });

  it("shows the same not-found text for an item that is not a copy or that the caller cannot see", async () => {
    redaction.mockRejectedValue(new ApiError(404, "evidence not found"));
    render(<RedactionBlock derivedId="99999999-9999-4999-8999-999999999999" />);

    expect(await screen.findByText("No evidence with that ID is available to your account.")).toBeInTheDocument();
  });
});
