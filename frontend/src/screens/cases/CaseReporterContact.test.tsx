import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, getReporterContact } from "../../api";
import { CaseReporterContact } from "./CaseReporterContact";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, getReporterContact: vi.fn() };
});

const read = vi.mocked(getReporterContact);

beforeEach(() => {
  read.mockReset();
});

describe("CaseReporterContact", () => {
  it("does not read the contact on page load", () => {
    render(<CaseReporterContact caseId="case-1" />);

    expect(read).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Show reporter contact" })).toBeEnabled();
  });

  it("shows the audit note next to the button", () => {
    render(<CaseReporterContact caseId="case-1" />);

    expect(screen.getByText("Reading the contact is recorded in the audit log.")).toBeInTheDocument();
  });

  it("reads the contact on click and shows it", async () => {
    read.mockResolvedValue("example-reporter@example.com");
    const user = userEvent.setup();
    render(<CaseReporterContact caseId="case-1" />);

    await user.click(screen.getByRole("button", { name: "Show reporter contact" }));

    expect(await screen.findByText("example-reporter@example.com")).toBeInTheDocument();
    expect(read).toHaveBeenCalledTimes(1);
    expect(read).toHaveBeenCalledWith("case-1");
    expect(screen.queryByRole("button", { name: "Show reporter contact" })).not.toBeInTheDocument();
  });

  it("shows No contact recorded when the case has no contact", async () => {
    read.mockResolvedValue(null);
    const user = userEvent.setup();
    render(<CaseReporterContact caseId="case-1" />);

    await user.click(screen.getByRole("button", { name: "Show reporter contact" }));

    expect(await screen.findByText("No contact recorded")).toBeInTheDocument();
  });

  it("shows the server message and keeps the button when the read fails", async () => {
    read.mockRejectedValueOnce(new ApiError(404, "case not found")).mockResolvedValueOnce("example-reporter@example.com");
    const user = userEvent.setup();
    render(<CaseReporterContact caseId="case-1" />);

    await user.click(screen.getByRole("button", { name: "Show reporter contact" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("No case with that ID is available to your account.");

    await user.click(screen.getByRole("button", { name: "Show reporter contact" }));

    await waitFor(() => expect(read).toHaveBeenCalledTimes(2));
    expect(await screen.findByText("example-reporter@example.com")).toBeInTheDocument();
  });
});
