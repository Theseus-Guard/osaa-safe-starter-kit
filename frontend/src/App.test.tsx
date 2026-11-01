import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { me, onSessionEnded, readiness } from "./api";
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
  };
});

beforeEach(() => {
  vi.mocked(readiness).mockResolvedValue("ready");
});

describe("App", () => {
  it("renders the login page when there is no session", async () => {
    render(<App />);

    expect(await screen.findByText("Sign in to continue")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Log in" })).toBeInTheDocument();
    expect(me).not.toHaveBeenCalled();
    expect(onSessionEnded).toHaveBeenCalledTimes(1);
  });
});
