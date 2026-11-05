import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { CaseLink, listCaseLinks } from "../../api";
import { CaseLinkedCases } from "./CaseLinkedCases";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, listCaseLinks: vi.fn() };
});

const list = vi.mocked(listCaseLinks);

const BASE: CaseLink = {
  linked_case_id: "3f2a9c1e-4b7d-4e8a-9c2f-1a2b3c4d5e6f",
  linked_case_title: "Ministry portal exposure",
  link_type: "duplicate",
  reason: "Same exposure",
  share_visibility: false,
  primary_side: null,
  primary_case_id: null,
  created_at: "2026-11-01T06:00:00Z",
};

beforeEach(() => {
  list.mockReset();
});

describe("CaseLinkedCases", () => {
  it("shows nothing when the case has no links", async () => {
    list.mockResolvedValue([]);
    const { container } = render(<CaseLinkedCases caseId="case-1" />);
    await vi.waitFor(() => expect(list).toHaveBeenCalledWith("case-1"));
    expect(container).toBeEmptyDOMElement();
  });

  it("lists a visible linked case with a link to it and its type", async () => {
    list.mockResolvedValue([BASE]);
    render(<CaseLinkedCases caseId="case-1" />);
    const link = await screen.findByRole("link", { name: "Ministry portal exposure" });
    expect(link).toHaveAttribute("href", `#/cases/${BASE.linked_case_id}`);
    expect(screen.getByText("Duplicate")).toBeInTheDocument();
  });

  it("counts the links the account cannot see without naming them", async () => {
    list.mockResolvedValue([{ ...BASE, linked_case_id: null, linked_case_title: null }]);
    render(<CaseLinkedCases caseId="case-1" />);
    expect(await screen.findByText("1 linked case is not available to your account.")).toBeInTheDocument();
  });
});
