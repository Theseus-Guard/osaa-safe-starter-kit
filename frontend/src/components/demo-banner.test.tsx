import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { demoStatus } from "../api";
import { DemoBanner } from "./demo-banner";

vi.mock("../api", () => ({
  demoStatus: vi.fn(),
}));

describe("DemoBanner", () => {
  beforeEach(() => {
    vi.mocked(demoStatus).mockReset();
  });

  it("shows the demo text when the server reports demo mode", async () => {
    vi.mocked(demoStatus).mockResolvedValue({ demo: true });
    render(<DemoBanner />);
    expect(await screen.findByText("DEMO / SYNTHETIC DATA")).toBeInTheDocument();
  });

  it("renders nothing when the server reports production", async () => {
    vi.mocked(demoStatus).mockResolvedValue({ demo: false });
    const { container } = render(<DemoBanner />);
    await vi.waitFor(() => expect(demoStatus).toHaveBeenCalled());
    await vi.waitFor(() => expect(container).toBeEmptyDOMElement());
  });

  it("renders nothing when the status request fails", async () => {
    vi.mocked(demoStatus).mockRejectedValue(new Error("unreachable"));
    const { container } = render(<DemoBanner />);
    await vi.waitFor(() => expect(demoStatus).toHaveBeenCalled());
    expect(container).toBeEmptyDOMElement();
  });
});
