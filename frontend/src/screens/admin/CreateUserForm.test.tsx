import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, createUser } from "../../api";
import { CreateUserForm } from "./CreateUserForm";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, createUser: vi.fn() };
});

beforeEach(() => {
  vi.mocked(createUser).mockReset();
});

describe("CreateUserForm", () => {
  it("keeps focus on the submit button after a server error, so the keyboard user is not dropped to the page top", async () => {
    vi.mocked(createUser).mockRejectedValue(new ApiError(409, "username already exists"));
    const user = userEvent.setup();
    render(<CreateUserForm onCreated={() => {}} />);

    await user.type(screen.getByLabelText(/Username/), "demo-admin");
    await user.type(screen.getByLabelText(/Email/), "probe@example.com");
    await user.type(screen.getByLabelText(/Temporary password/), "a-long-temporary-password");
    const submit = screen.getByRole("button", { name: "Create user" });
    submit.focus();
    await user.keyboard("{Enter}");

    expect(await screen.findByRole("alert")).toHaveTextContent("That username is already taken.");
    await waitFor(() => expect(submit).toHaveFocus());
    expect(submit).toBeEnabled();
  });
});
