/**
 * Basic accessibility checks for the main pages (F2 T15): every form control has an accessible name, and an error
 * message is tied to the field it is about (aria-invalid on the field, aria-describedby pointing at the message).
 * They use the testing library queries only; no accessibility tool is added.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createReport, demoStatus, listCases, listEvidence, readiness } from "./api";
import { Login } from "./screens/Login";
import { CasesPage } from "./screens/cases/CasesPage";
import { CaseEvidence } from "./screens/cases/CaseEvidence";
import { NewCaseDialog } from "./screens/cases/NewCaseDialog";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    readiness: vi.fn(),
    demoStatus: vi.fn(),
    demoSignIn: vi.fn(),
    listCases: vi.fn(),
    listEvidence: vi.fn(),
    createReport: vi.fn(),
  };
});

vi.mock("./system-timezone", () => ({
  useSystemTimezone: () => ({
    timezone: "Europe/Istanbul",
    effectiveFrom: null,
    error: null,
    setCurrent: () => {},
    reload: () => {},
  }),
}));

// Istanbul is UTC+03:00. The clock is fixed, so the awareness time check is the same on every run.
const NOW = new Date("2026-11-01T06:00:00Z");

beforeEach(() => {
  vi.mocked(readiness).mockResolvedValue("ready");
  vi.mocked(demoStatus).mockResolvedValue({ demo: false, users: [] });
  vi.mocked(listCases).mockResolvedValue({ items: [], next_cursor: null });
  vi.mocked(createReport).mockReset();
  vi.mocked(listEvidence).mockResolvedValue({ items: [], total: 0, limit: 50, offset: 0 });
});

describe("labelled form controls on the main pages", () => {
  it("the login form labels the username and password fields", async () => {
    render(<Login notice={null} onLogin={vi.fn().mockResolvedValue(undefined)} onDemoSignIn={vi.fn()} />);

    expect(await screen.findByLabelText("Username")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toHaveAttribute("type", "password");
    expect(screen.getByRole("button", { name: "Show password" })).toBeInTheDocument();
  });

  it("the case list labels its search box and its status filter", async () => {
    render(<CasesPage permissions={["cases:read_all", "cases:submit"]} />);

    expect(await screen.findByRole("searchbox", { name: "Search cases" })).toBeInTheDocument();
    expect(screen.getByRole("combobox", { name: "Filter by status" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "New case" })).toBeInTheDocument();
  });

  it("the evidence panel labels its file input", async () => {
    render(<CaseEvidence caseId="case-1" canRead canWrite />);

    expect(await screen.findByLabelText("Upload evidence")).toHaveAttribute("type", "file");
  });

  it("the new case form labels every field", async () => {
    render(<NewCaseDialog open onOpenChange={vi.fn()} onCreated={vi.fn()} />);

    expect(await screen.findByLabelText("Title")).toBeInTheDocument();
    expect(screen.getByLabelText("Awareness date and time")).toHaveAttribute("type", "date");
    expect(screen.getByLabelText("Awareness time")).toHaveAttribute("type", "time");
    expect(screen.getByLabelText("Contact for follow-up")).toBeInTheDocument();
  });
});

describe("error messages tied to their fields", () => {
  beforeEach(() => {
    // Only Date is faked, so user-event timers keep running.
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(NOW);
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("marks the title as invalid and points it at the message when the title is only spaces", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    render(<NewCaseDialog open onOpenChange={vi.fn()} onCreated={vi.fn()} />);

    await user.type(screen.getByLabelText("Title"), "   ");
    await user.click(screen.getByRole("button", { name: "Create case" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Enter a title for the case.");
    const title = screen.getByLabelText("Title");
    expect(title).toHaveAttribute("aria-invalid", "true");
    expect(title).toHaveAttribute("aria-describedby", alert.id);
    expect(alert.id).not.toBe("");
    expect(vi.mocked(createReport)).not.toHaveBeenCalled();
  });

  it("marks the awareness fields as invalid and points them at the message for a future time", async () => {
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    render(<NewCaseDialog open onOpenChange={vi.fn()} onCreated={vi.fn()} />);

    await user.type(screen.getByLabelText("Title"), "Example incident");
    fireEvent.change(screen.getByLabelText("Awareness time"), { target: { value: "23:00" } });
    await user.click(screen.getByRole("button", { name: "Create case" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The awareness time cannot be in the future.");
    await waitFor(() => expect(screen.getByLabelText("Awareness time")).toHaveAttribute("aria-invalid", "true"));
    expect(screen.getByLabelText("Awareness time")).toHaveAttribute("aria-describedby", alert.id);
    expect(screen.getByLabelText("Awareness date and time")).toHaveAttribute("aria-describedby", alert.id);
    expect(screen.getByLabelText("Title")).not.toHaveAttribute("aria-invalid");
  });
});
