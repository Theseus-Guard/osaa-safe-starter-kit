import { render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { listPermissions, listRoles, PermissionSummary, ROLES, RoleSummary } from "../../api";
import { RolesMatrix } from "./RolesMatrix";

vi.mock("../../api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../api")>();
  return { ...actual, listRoles: vi.fn(), listPermissions: vi.fn(), updateRolePermissions: vi.fn() };
});

const roles: RoleSummary[] = [
  { role: "triager", permissions: ["cases:triage"] },
  { role: "reporting_owner", permissions: ["cases:read_all", "reporter_contact:read"] },
  { role: "admin", permissions: ["users:manage", "users:reset_password", "roles:manage"] },
];

const catalog: PermissionSummary[] = [
  { name: "cases:read_all", description: "Read every case" },
  { name: "cases:triage", description: "Triage cases" },
  { name: "reporter_contact:read", description: "Read the contact a reporter gave" },
  { name: "users:manage", description: "Manage users" },
  { name: "users:reset_password", description: "Reset passwords" },
  { name: "roles:manage", description: "Change role permissions" },
];

beforeEach(() => {
  vi.mocked(listRoles).mockReset().mockResolvedValue(roles);
  vi.mocked(listPermissions).mockReset().mockResolvedValue(catalog);
});

describe("role list", () => {
  it("includes the reporting_owner role exactly once", () => {
    expect(ROLES.filter((role) => role === "reporting_owner")).toHaveLength(1);
  });
});

describe("RolesMatrix with the reporting_owner row", () => {
  it("shows the fixed row with every checkbox locked and the save button disabled", async () => {
    render(<RolesMatrix editable />);

    const row = await screen.findByRole("row", { name: /reporting_owner/ });

    expect(within(row).getByText("This role is fixed. Its grants cannot be changed here.")).toBeInTheDocument();
    for (const name of ["reporting_owner cases:read_all", "reporting_owner reporter_contact:read"]) {
      const checkbox = screen.getByRole("checkbox", { name });
      expect(checkbox).toBeChecked();
      // The checkbox is a span, so the locked state is exposed as aria-disabled rather than the disabled attribute.
      expect(checkbox).toHaveAttribute("aria-disabled", "true");
    }
    expect(within(row).getByRole("button", { name: "Save" })).toBeDisabled();
  });

  it("keeps the other rows editable", async () => {
    render(<RolesMatrix editable />);

    await screen.findByRole("row", { name: /reporting_owner/ });

    expect(screen.getByRole("checkbox", { name: "triager cases:triage" })).toBeEnabled();
  });

  it("shows the fixed row read-only when the viewer is not an administrator", async () => {
    render(<RolesMatrix editable={false} />);

    expect(await screen.findByText("cases:read_all, reporter_contact:read")).toBeInTheDocument();
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });
});
