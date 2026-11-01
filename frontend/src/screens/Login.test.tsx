import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, readiness } from "../api";
import { Login } from "./Login";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, readiness: vi.fn() };
});

const ready = vi.mocked(readiness);

function renderLogin(onLogin = vi.fn().mockResolvedValue(undefined), notice: string | null = null) {
  render(<Login notice={notice} onLogin={onLogin} />);
  return { onLogin };
}

beforeEach(() => {
  ready.mockReset();
  ready.mockResolvedValue("ready");
});

describe("Login", () => {
  it("renders the sign-in form", async () => {
    renderLogin();

    expect(screen.getByRole("heading", { level: 1, name: "SAFE-Starter-Kit" })).toBeInTheDocument();
    expect(screen.getByLabelText("Username")).toBeEnabled();
    expect(screen.getByLabelText("Password")).toBeEnabled();
    expect(await screen.findByRole("button", { name: "Log in" })).toBeEnabled();
  });

  it("passes the entered credentials to onLogin", async () => {
    const user = userEvent.setup();
    const { onLogin } = renderLogin();

    await user.type(screen.getByLabelText("Username"), "example-user");
    await user.type(screen.getByLabelText("Password"), "example-password");
    await user.click(await screen.findByRole("button", { name: "Log in" }));

    expect(onLogin).toHaveBeenCalledWith("example-user", "example-password");
  });

  it("shows the error alert for a rejected login", async () => {
    const user = userEvent.setup();
    renderLogin(vi.fn().mockRejectedValue(new ApiError(401, "invalid credentials")));

    await user.type(screen.getByLabelText("Username"), "example-user");
    await user.type(screen.getByLabelText("Password"), "wrong-example-password");
    await user.click(await screen.findByRole("button", { name: "Log in" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Sign-in failed");
    expect(alert).toHaveTextContent("Invalid username or password.");
  });

  it("shows a network failure message when the server cannot be reached", async () => {
    const user = userEvent.setup();
    renderLogin(vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    await user.type(screen.getByLabelText("Username"), "example-user");
    await user.type(screen.getByLabelText("Password"), "example-password");
    await user.click(await screen.findByRole("button", { name: "Log in" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("The server could not be reached.");
  });

  it("shows the setup warning instead of a red error when login returns 503", async () => {
    const user = userEvent.setup();
    renderLogin(vi.fn().mockRejectedValue(new ApiError(503, "system setup required")));

    await user.type(screen.getByLabelText("Username"), "example-user");
    await user.type(screen.getByLabelText("Password"), "example-password");
    await user.click(await screen.findByRole("button", { name: "Log in" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Setup required");
    expect(alert).toHaveTextContent("System setup required.");
  });

  it("links to the Alliance pages in a new tab without leaking the opener", async () => {
    renderLogin();
    await screen.findByRole("button", { name: "Log in" });

    const links = [
      screen.getByRole("link", { name: "Open Secure AI Alliance official repo" }),
      screen.getByRole("link", { name: "Open Secure AI Alliance (OSAA)" }),
    ];
    for (const link of links) {
      expect(link).toHaveAttribute("target", "_blank");
      expect(link).toHaveAttribute("rel", "noopener noreferrer");
    }
    expect(links[0]).toHaveAttribute("href", "https://github.com/OpenSecureAIAlliance/wg-safe");
    expect(links[1]).toHaveAttribute("href", "https://secureaialliance.org/");
  });

  it("disables the form and shows a warning when the system needs setup", async () => {
    ready.mockResolvedValue("setup_required");
    renderLogin();

    expect(await screen.findByText("Setup required")).toBeInTheDocument();
    expect(screen.getByLabelText("Username")).toBeDisabled();
    expect(screen.getByLabelText("Password")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Log in" })).toBeDisabled();
  });

  it("disables the form when the service cannot be reached", async () => {
    ready.mockRejectedValue(new TypeError("Failed to fetch"));
    renderLogin();

    expect(await screen.findByText("Service unavailable")).toBeInTheDocument();
    expect(screen.getByLabelText("Username")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Log in" })).toBeDisabled();
  });

  it("shows the notice passed by the app", async () => {
    renderLogin(undefined, "Your session has ended. Please log in again.");

    expect(screen.getByText("Your session has ended. Please log in again.")).toBeInTheDocument();
    await screen.findByRole("button", { name: "Log in" });
  });

  it("toggles password visibility", async () => {
    const user = userEvent.setup();
    renderLogin();

    const password = screen.getByLabelText("Password");
    expect(password).toHaveAttribute("type", "password");

    await user.click(screen.getByRole("button", { name: "Show password" }));
    expect(password).toHaveAttribute("type", "text");
    expect(screen.getByRole("button", { name: "Hide password" })).toHaveAttribute("aria-pressed", "true");
  });
});
