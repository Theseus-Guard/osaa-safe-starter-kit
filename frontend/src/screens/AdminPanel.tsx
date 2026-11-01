import { useState } from "react";
import { CreateUserForm } from "./admin/CreateUserForm";
import { RolesMatrix } from "./admin/RolesMatrix";
import { UserManagement } from "./admin/UserManagement";

interface Props {
  currentUserId: string;
}

export function AdminPanel({ currentUserId }: Props) {
  // Any user change bumps this counter, so the user list reloads its current page.
  const [revision, setRevision] = useState(0);
  const handleChanged = () => setRevision((value) => value + 1);

  return (
    <>
      <CreateUserForm onCreated={handleChanged} />
      <UserManagement currentUserId={currentUserId} revision={revision} onChanged={handleChanged} />
      <RolesMatrix />
    </>
  );
}
