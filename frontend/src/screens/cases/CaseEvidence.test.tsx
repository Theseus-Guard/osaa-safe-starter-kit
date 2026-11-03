import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { toast } from "sonner";
import {
  EvidenceItem,
  EvidencePage,
  downloadEvidence,
  listCustodyEvents,
  listEvidence,
  saveBlob,
  uploadEvidence,
} from "../../api";
import { CaseEvidence } from "./CaseEvidence";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return {
    ...actual,
    listEvidence: vi.fn(),
    uploadEvidence: vi.fn(),
    downloadEvidence: vi.fn(),
    listCustodyEvents: vi.fn(),
    saveBlob: vi.fn(),
  };
});

vi.mock("../../system-timezone", () => ({
  useSystemTimezone: () => ({ timezone: "UTC", effectiveFrom: null, error: null, setCurrent: () => {}, reload: () => {} }),
}));

vi.mock("sonner", () => ({ toast: { success: vi.fn(), error: vi.fn() } }));

const list = vi.mocked(listEvidence);
const upload = vi.mocked(uploadEvidence);
const download = vi.mocked(downloadEvidence);
const custody = vi.mocked(listCustodyEvents);
const save = vi.mocked(saveBlob);

const ITEM: EvidenceItem = {
  id: "evidence-1",
  case_id: "case-1",
  uploaded_by: "user-2",
  original_filename: "notes.txt",
  media_type: "text/plain",
  size_bytes: 2048,
  sha256_plaintext: "0123456789abcdef0123456789abcdef",
  status: "available",
  created_at: "2026-11-01T06:00:00Z",
};

function evidencePage(items: EvidenceItem[]): EvidencePage {
  return { items, total: items.length, limit: 20, offset: 0 };
}

beforeEach(() => {
  list.mockReset();
  upload.mockReset();
  download.mockReset();
  custody.mockReset();
  save.mockReset();
  vi.mocked(toast.success).mockClear();
  vi.mocked(toast.error).mockClear();
});

describe("CaseEvidence", () => {
  it("shows an empty state when no evidence exists", async () => {
    list.mockResolvedValue(evidencePage([]));
    render(<CaseEvidence caseId="case-1" canRead canWrite />);

    expect(await screen.findByText("No evidence uploaded yet.")).toBeInTheDocument();
  });

  it("lists the file with its size, status and a shortened checksum", async () => {
    list.mockResolvedValue(evidencePage([ITEM]));
    render(<CaseEvidence caseId="case-1" canRead canWrite />);

    const row = (await screen.findByText("notes.txt")).closest("tr") as HTMLElement;
    expect(within(row).getByText("2.0 KB")).toBeInTheDocument();
    expect(within(row).getByText("Available")).toBeInTheDocument();
    expect(within(row).getByText("0123456789ab…")).toBeInTheDocument();
  });

  it("keeps the upload button disabled until a file is chosen", async () => {
    list.mockResolvedValue(evidencePage([]));
    render(<CaseEvidence caseId="case-1" canRead canWrite />);

    expect(await screen.findByRole("button", { name: "Upload" })).toBeDisabled();
  });

  it("uploads the chosen file and reloads the list", async () => {
    list.mockResolvedValueOnce(evidencePage([])).mockResolvedValueOnce(evidencePage([ITEM]));
    upload.mockResolvedValue(ITEM);
    const user = userEvent.setup();
    render(<CaseEvidence caseId="case-1" canRead canWrite />);

    const file = new File(["example text"], "notes.txt", { type: "text/plain" });
    await user.upload(await screen.findByLabelText(/Upload evidence/), file);
    await user.click(screen.getByRole("button", { name: "Upload" }));

    await waitFor(() => expect(upload).toHaveBeenCalledWith("case-1", file));
    expect(await screen.findByText("notes.txt")).toBeInTheDocument();
    expect(toast.success).toHaveBeenCalledWith("notes.txt was uploaded.");
  });

  it("shows the server message when the upload is refused", async () => {
    const { ApiError } = await vi.importActual<typeof import("../../api")>("../../api");
    list.mockResolvedValue(evidencePage([]));
    upload.mockRejectedValue(new ApiError(415, "media type is not allowed"));
    // The file type is refused by the server, so the picker must not filter it out before the upload.
    const user = userEvent.setup({ applyAccept: false });
    render(<CaseEvidence caseId="case-1" canRead canWrite />);

    await user.upload(await screen.findByLabelText(/Upload evidence/), new File(["x"], "tool.exe"));
    await user.click(screen.getByRole("button", { name: "Upload" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("This file type is not allowed.");
  });

  it("downloads available evidence and keeps the original file name", async () => {
    const bytes = new Blob(["example text"]);
    list.mockResolvedValue(evidencePage([ITEM]));
    download.mockResolvedValue(bytes);
    const user = userEvent.setup();
    render(<CaseEvidence caseId="case-1" canRead canWrite />);

    await user.click(await screen.findByRole("button", { name: "Download" }));

    await waitFor(() => expect(save).toHaveBeenCalledWith(bytes, "notes.txt"));
    expect(download).toHaveBeenCalledWith("evidence-1");
  });

  it("does not offer a download for evidence that is not available", async () => {
    list.mockResolvedValue(evidencePage([{ ...ITEM, status: "pending" }]));
    render(<CaseEvidence caseId="case-1" canRead canWrite />);

    expect(await screen.findByRole("button", { name: "Download" })).toBeDisabled();
  });

  it("shows the custody log of an item when it is opened", async () => {
    list.mockResolvedValue(evidencePage([ITEM]));
    custody.mockResolvedValue([
      {
        id: "event-1",
        action: "uploaded",
        actor_id: "user-2",
        details: { size_bytes: 2048 },
        created_at: "2026-11-01T06:00:00Z",
      },
    ]);
    const user = userEvent.setup();
    render(<CaseEvidence caseId="case-1" canRead canWrite />);

    await user.click(await screen.findByRole("button", { name: "Custody" }));

    const log = await screen.findByRole("list");
    expect(within(log).getByText(/Uploaded/)).toBeInTheDocument();
    expect(within(log).getByText("Size bytes: 2048")).toBeInTheDocument();
    expect(custody).toHaveBeenCalledWith("evidence-1");
    expect(screen.getByRole("button", { name: "Hide custody" })).toBeInTheDocument();
  });

  it("does not request the list or offer downloads and custody without evidence:read", async () => {
    render(<CaseEvidence caseId="case-1" canRead={false} canWrite />);

    expect(await screen.findByLabelText(/Upload evidence/)).toBeInTheDocument();
    expect(list).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "Download" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Custody" })).not.toBeInTheDocument();
  });

  it("hides the upload form without evidence:write but keeps the list", async () => {
    list.mockResolvedValue(evidencePage([ITEM]));
    render(<CaseEvidence caseId="case-1" canRead canWrite={false} />);

    expect(await screen.findByText("notes.txt")).toBeInTheDocument();
    expect(screen.queryByLabelText(/Upload evidence/)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Upload" })).not.toBeInTheDocument();
  });

  it("shows the server message when the list is refused", async () => {
    const { ApiError } = await vi.importActual<typeof import("../../api")>("../../api");
    list.mockRejectedValue(new ApiError(403, "forbidden"));
    render(<CaseEvidence caseId="case-1" canRead canWrite={false} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("You do not have permission for this action.");
  });

  it("shows a failed item with its own status and offers no download for it", async () => {
    list.mockResolvedValue(evidencePage([{ ...ITEM, status: "failed" }]));
    render(<CaseEvidence caseId="case-1" canRead canWrite />);

    const row = (await screen.findByText("notes.txt")).closest("tr") as HTMLElement;
    expect(within(row).getByText("Failed")).toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Download" })).toBeDisabled();
    expect(screen.queryByText("No evidence uploaded yet.")).not.toBeInTheDocument();
  });

  it("shows a pending item as Pending, not as available, and offers no download for it", async () => {
    list.mockResolvedValue(evidencePage([{ ...ITEM, status: "pending" }]));
    render(<CaseEvidence caseId="case-1" canRead canWrite />);

    const row = (await screen.findByText("notes.txt")).closest("tr") as HTMLElement;
    expect(within(row).getByText("Pending")).toBeInTheDocument();
    expect(within(row).queryByText("Available")).not.toBeInTheDocument();
    expect(within(row).getByRole("button", { name: "Download" })).toBeDisabled();
  });

  it("does not show the empty state when the list cannot be loaded, and names the refusal", async () => {
    const { ApiError } = await vi.importActual<typeof import("../../api")>("../../api");
    list.mockRejectedValue(new ApiError(404, "case not found"));
    render(<CaseEvidence caseId="case-1" canRead canWrite={false} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("No case with that ID is available to your account.");
    expect(screen.queryByText("No evidence uploaded yet.")).not.toBeInTheDocument();
  });

  it("never reads an empty case as a pass or a verified result", async () => {
    list.mockResolvedValue(evidencePage([]));
    render(<CaseEvidence caseId="case-1" canRead canWrite={false} />);

    expect(await screen.findByText("No evidence uploaded yet.")).toBeInTheDocument();
    expect(screen.queryByText(/\bpass(ed)?\b|verified|complete/i)).not.toBeInTheDocument();
  });

  it("says the file is not available when the server refuses a download of an available item", async () => {
    const { ApiError } = await vi.importActual<typeof import("../../api")>("../../api");
    list.mockResolvedValue(evidencePage([ITEM]));
    download.mockRejectedValue(new ApiError(409, "evidence is not available"));
    const user = userEvent.setup();
    render(<CaseEvidence caseId="case-1" canRead canWrite />);

    await user.click(await screen.findByRole("button", { name: "Download" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith("This evidence is not available for download yet."),
    );
    expect(save).not.toHaveBeenCalled();
  });
});
