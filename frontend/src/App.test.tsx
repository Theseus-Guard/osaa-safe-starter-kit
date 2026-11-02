import { StrictMode } from "react";
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { login, me, onSessionEnded, readiness, restoreSession, type Me } from "./api";
import { App } from "./App";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return {
    ...actual,
    readiness: vi.fn(),
    login: vi.fn(),
    me: vi.fn(),
    logout: vi.fn(),
    changePassword: vi.fn(),
    onSessionEnded: vi.fn(),
    restoreSession: vi.fn(),
  };
});

// The dashboard loads many endpoints on mount. These tests only care which shell is shown.
vi.mock("./screens/Home", () => ({
  Home: ({ profile }: { profile: { username: string } }) => <p>Dashboard for {profile.username}</p>,
}));

const ME: Me = {
  id: "user-1",
  username: "example-user",
  role: "reporter",
  first_name: null,
  last_name: null,
  title: null,
  email: "user@example.com",
  password_change_required: false,
  timezone_setup_required: false,
  permissions: [],
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.mocked(readiness).mockResolvedValue("ready");
  // By default there is no session to restore.
  vi.mocked(restoreSession).mockResolvedValue(null);
});

describe("App", () => {
  it("renders the login page when there is no session", async () => {
    render(<App />);

    expect(await screen.findByText("Sign in to continue")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Log in" })).toBeInTheDocument();
    expect(restoreSession).toHaveBeenCalledTimes(1);
    expect(me).not.toHaveBeenCalled();
    expect(onSessionEnded).toHaveBeenCalledTimes(1);
  });

  it("shows the dashboard after a reload when the browser session is still valid", async () => {
    vi.mocked(restoreSession).mockResolvedValue(ME);

    render(<App />);

    expect(await screen.findByText("Dashboard for example-user")).toBeInTheDocument();
    expect(screen.queryByText("Sign in to continue")).not.toBeInTheDocument();
    expect(login).not.toHaveBeenCalled();
  });

  it("shows the forced password change page when must_change_password is set", async () => {
    vi.mocked(restoreSession).mockResolvedValue({ ...ME, password_change_required: true });

    render(<App />);

    expect(await screen.findByText("Choose a new password")).toBeInTheDocument();
    expect(screen.queryByText("Dashboard for example-user")).not.toBeInTheDocument();
    expect(screen.queryByText("Sign in to continue")).not.toBeInTheDocument();
  });

  it("shows the login page when the start-up check cannot restore a session", async () => {
    vi.mocked(restoreSession).mockRejectedValue(new TypeError("network down"));

    render(<App />);

    expect(await screen.findByText("Sign in to continue")).toBeInTheDocument();
    expect(screen.queryByText(/Dashboard for/)).not.toBeInTheDocument();
  });

  it("runs the start-up check once even when effects run twice", async () => {
    vi.mocked(restoreSession).mockResolvedValue(ME);

    render(
      <StrictMode>
        <App />
      </StrictMode>,
    );
    await screen.findByText("Dashboard for example-user");

    expect(restoreSession).toHaveBeenCalledTimes(1);
  });
});
