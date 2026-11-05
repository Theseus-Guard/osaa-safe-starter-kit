import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, demoSignIn, demoStatus, DemoStatus, readiness } from "../api";
import { Login } from "./Login";

vi.mock("../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api")>();
  return { ...actual, readiness: vi.fn(), demoStatus: vi.fn(), demoSignIn: vi.fn() };
});

const ready = vi.mocked(readiness);
const demo = vi.mocked(demoStatus);

const PRODUCTION: DemoStatus = { demo: false, users: [] };
const DEMO_ROLES: DemoStatus = {
  demo: true,
  users: [
    { username: "demo-admin", name: "Demo Administrator", role: "admin" },
    { username: "demo-reporter", name: "Demo Reporter", role: "reporter" },
    { username: "demo-triager", name: "Demo Triager", role: "triager" },
  ],
};
const EXAMPLE_CREDENTIAL = "example-demo-credential-7f3a";

function renderLogin(
  onLogin = vi.fn().mockResolvedValue(undefined),
  notice: string | null = null,
  onDemoSignIn = vi.fn().mockResolvedValue(undefined),
) {
  render(<Login notice={notice} onLogin={onLogin} onDemoSignIn={onDemoSignIn} />);
  return { onLogin, onDemoSignIn };
}

beforeEach(() => {
  ready.mockReset();
  ready.mockResolvedValue("ready");
  demo.mockReset();
  demo.mockResolvedValue(PRODUCTION);
  vi.mocked(demoSignIn).mockReset();
});

describe("Login", () => {
  it("renders the sign-in form", async () => {
    renderLogin();

    expect(screen.getByRole("heading", { level: 1, name: "SAFE-Starter-Kit" })).toBeInTheDocument();
    expect(screen.getByLabelText(/^Username/)).toBeEnabled();
    expect(screen.getByLabelText(/^Password/)).toBeEnabled();
    expect(await screen.findByRole("button", { name: "Log in" })).toBeEnabled();
  });

  it("passes the entered credentials to onLogin", async () => {
    const user = userEvent.setup();
    const { onLogin } = renderLogin();

    await user.type(screen.getByLabelText(/^Username/), "example-user");
    await user.type(screen.getByLabelText(/^Password/), "example-password");
    await user.click(await screen.findByRole("button", { name: "Log in" }));

    expect(onLogin).toHaveBeenCalledWith("example-user", "example-password");
  });

  it("marks an empty username and password on the page and does not send the login", async () => {
    const user = userEvent.setup();
    const { onLogin } = renderLogin();

    await user.click(await screen.findByRole("button", { name: "Log in" }));

    const username = screen.getByLabelText(/^Username/);
    expect(username).toHaveAttribute("aria-invalid", "true");
    expect(username).toHaveAccessibleDescription("Enter your username.");
    expect(screen.getByLabelText(/^Password/)).toHaveAccessibleDescription("Enter your password.");
    expect(username).toHaveFocus();
    expect(onLogin).not.toHaveBeenCalled();
  });

  it("marks an empty demo credential on the page and does not sign in", async () => {
    const user = userEvent.setup();
    demo.mockResolvedValue(DEMO_ROLES);
    const { onDemoSignIn } = renderLogin();

    await user.click(await screen.findByRole("button", { name: "Log in" }));

    const credential = screen.getByLabelText(/^Demo credential/);
    expect(credential).toHaveAttribute("aria-invalid", "true");
    expect(credential).toHaveAccessibleDescription("Enter the demo credential for this role.");
    expect(onDemoSignIn).not.toHaveBeenCalled();
  });

  it("shows the error alert for a rejected login", async () => {
    const user = userEvent.setup();
    renderLogin(vi.fn().mockRejectedValue(new ApiError(401, "invalid credentials")));

    await user.type(screen.getByLabelText(/^Username/), "example-user");
    await user.type(screen.getByLabelText(/^Password/), "wrong-example-password");
    await user.click(await screen.findByRole("button", { name: "Log in" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Sign-in failed");
    expect(alert).toHaveTextContent("Invalid username or password.");
  });

  it("shows a network failure message when the server cannot be reached", async () => {
    const user = userEvent.setup();
    renderLogin(vi.fn().mockRejectedValue(new TypeError("Failed to fetch")));

    await user.type(screen.getByLabelText(/^Username/), "example-user");
    await user.type(screen.getByLabelText(/^Password/), "example-password");
    await user.click(await screen.findByRole("button", { name: "Log in" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("The server could not be reached.");
  });

  it("shows the setup warning instead of a red error when login returns 503", async () => {
    const user = userEvent.setup();
    renderLogin(vi.fn().mockRejectedValue(new ApiError(503, "system setup required")));

    await user.type(screen.getByLabelText(/^Username/), "example-user");
    await user.type(screen.getByLabelText(/^Password/), "example-password");
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
    expect(screen.getByLabelText(/^Username/)).toBeDisabled();
    expect(screen.getByLabelText(/^Password/)).toBeDisabled();
    expect(screen.getByRole("button", { name: "Log in" })).toBeDisabled();
  });

  it("disables the form when the service cannot be reached", async () => {
    ready.mockRejectedValue(new TypeError("Failed to fetch"));
    renderLogin();

    expect(await screen.findByText("Service unavailable")).toBeInTheDocument();
    expect(screen.getByLabelText(/^Username/)).toBeDisabled();
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

    const password = screen.getByLabelText(/^Password/);
    expect(password).toHaveAttribute("type", "password");

    // The name stays "Show password"; the pressed state carries the toggle.
    const toggle = screen.getByRole("button", { name: "Show password" });
    await user.click(toggle);
    expect(password).toHaveAttribute("type", "text");
    expect(toggle).toHaveAttribute("aria-pressed", "true");
  });
});

describe("Login in demo mode", () => {
  it("shows the role picker and the credential field only in demo mode", async () => {
    demo.mockResolvedValue(DEMO_ROLES);
    renderLogin();

    const picker = await screen.findByLabelText("Demo role");
    expect(picker.tagName).toBe("SELECT");
    expect(screen.getByLabelText(/^Demo credential/)).toHaveAttribute("type", "password");
    expect(screen.queryByLabelText(/^Username/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/^Password/)).not.toBeInTheDocument();
  });

  it("does not show the picker outside demo mode", async () => {
    renderLogin();

    await screen.findByRole("button", { name: "Log in" });
    await vi.waitFor(() => expect(demo).toHaveBeenCalled());
    expect(screen.queryByLabelText("Demo role")).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/^Demo credential/)).not.toBeInTheDocument();
    expect(screen.getByLabelText(/^Username/)).toBeEnabled();
    expect(screen.getByLabelText(/^Password/)).toBeEnabled();
  });

  it("lists the demo roles by name and role, in the server's order", async () => {
    demo.mockResolvedValue(DEMO_ROLES);
    renderLogin();

    const options = within(await screen.findByLabelText("Demo role")).getAllByRole("option");
    expect(options.map((option) => option.textContent)).toEqual([
      "Demo Administrator (admin)",
      "Demo Reporter (reporter)",
      "Demo Triager (triager)",
    ]);
    expect(options.map((option) => option.getAttribute("value"))).toEqual(["demo-admin", "demo-reporter", "demo-triager"]);
  });

  it("signs in the chosen role with the entered credential", async () => {
    const user = userEvent.setup();
    demo.mockResolvedValue(DEMO_ROLES);
    const { onDemoSignIn, onLogin } = renderLogin();

    await user.selectOptions(await screen.findByLabelText("Demo role"), "demo-triager");
    await user.type(screen.getByLabelText(/^Demo credential/), EXAMPLE_CREDENTIAL);
    await user.click(screen.getByRole("button", { name: "Log in" }));

    expect(onDemoSignIn).toHaveBeenCalledWith("demo-triager", EXAMPLE_CREDENTIAL);
    expect(onLogin).not.toHaveBeenCalled();
  });

  it("keeps the credential out of the page text", async () => {
    const user = userEvent.setup();
    demo.mockResolvedValue(DEMO_ROLES);
    renderLogin();

    await screen.findByLabelText("Demo role");
    expect(document.body.innerHTML).not.toContain(EXAMPLE_CREDENTIAL);
    await user.type(screen.getByLabelText(/^Demo credential/), EXAMPLE_CREDENTIAL);
    await user.click(screen.getByRole("button", { name: "Log in" }));
    await vi.waitFor(() => expect(screen.getByRole("button", { name: "Log in" })).toBeEnabled());

    expect(document.body.textContent).not.toContain(EXAMPLE_CREDENTIAL);
  });

  it("shows the sign-in failure for a refused demo credential", async () => {
    const user = userEvent.setup();
    demo.mockResolvedValue(DEMO_ROLES);
    renderLogin(undefined, null, vi.fn().mockRejectedValue(new ApiError(401, "invalid credentials")));

    await user.type(await screen.findByLabelText(/^Demo credential/), EXAMPLE_CREDENTIAL);
    await user.click(screen.getByRole("button", { name: "Log in" }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Sign-in failed");
    expect(alert).toHaveTextContent("Invalid username or password.");
    expect(document.body.textContent).not.toContain(EXAMPLE_CREDENTIAL);
  });
});
