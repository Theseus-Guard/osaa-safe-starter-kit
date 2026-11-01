import { useState } from "react";
import { CreateUserForm } from "./CreateUserForm";
import { UserManagement } from "./UserManagement";

interface Props {
  currentUserId: string;
}

export function UsersPage({ currentUserId }: Props) {
  // Any user change bumps this counter, so the user list reloads its current page.
  const [revision, setRevision] = useState(0);
  const handleChanged = () => setRevision((value) => value + 1);

  return (
    <>
      <UserManagement currentUserId={currentUserId} revision={revision} onChanged={handleChanged} />
      <CreateUserForm onCreated={handleChanged} />
    </>
  );
}
