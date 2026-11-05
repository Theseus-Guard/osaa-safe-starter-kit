import { useEffect, useState } from "react";
import { toast } from "sonner";
import { AssignableUser, Assignment, addAssignment, listAssignableUsers, listAssignments, removeAssignment } from "../../api";
import { describeError } from "../../messages";
import { humanize } from "./case-format";
import { Timestamp } from "@/components/timestamp";
import { DisabledHint } from "@/components/disabled-hint";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldLabel } from "@/components/ui/field";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

interface Props {
  caseId: string;
  closed: boolean;
  // The caller has cases:assign. Without it removal is locked and the assign picker is not shown.
  canAssign: boolean;
}

// The name the picker shows: full name when the account has one, then the role.
function userLabel(user: AssignableUser): string {
  const fullName = [user.first_name, user.last_name].filter(Boolean).join(" ");
  const name = fullName ? `${fullName} (${user.username})` : user.username;
  return `${name} - ${humanize(user.role)}`;
}

export function CaseAssignments({ caseId, closed, canAssign }: Props) {
  const [items, setItems] = useState<Assignment[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyUserId, setBusyUserId] = useState<string | null>(null);
  // Bumped after a removal or an assignment so both lists reload from the server.
  const [version, setVersion] = useState(0);
  // The picker is offered only to a caller with cases:assign on an open case.
  const offerPicker = canAssign && !closed;
  const [candidates, setCandidates] = useState<AssignableUser[] | null>(null);
  const [pickerError, setPickerError] = useState<string | null>(null);
  const [selectedUserId, setSelectedUserId] = useState<string | null>(null);
  const [assigning, setAssigning] = useState(false);

  useEffect(() => {
    let stale = false;
    setError(null);
    listAssignments(caseId)
      .then((result) => {
        if (!stale) setItems(result);
      })
      .catch((caught: unknown) => {
        if (!stale) setError(describeError(caught));
      });
    return () => {
      stale = true;
    };
  }, [caseId, version]);

  useEffect(() => {
    if (!offerPicker) return;
    let stale = false;
    setPickerError(null);
    listAssignableUsers(caseId)
      .then((result) => {
        if (!stale) setCandidates(result);
      })
      .catch((caught: unknown) => {
        if (!stale) setPickerError(describeError(caught));
      });
    return () => {
      stale = true;
    };
  }, [caseId, offerPicker, version]);

  // Users already assigned are not offered again.
  const assignedUserIds = new Set((items ?? []).map((assignment) => assignment.user_id));
  const available = (candidates ?? []).filter((user) => !assignedUserIds.has(user.id));

  async function assign() {
    const user = available.find((candidate) => candidate.id === selectedUserId);
    if (!user) return;
    setAssigning(true);
    try {
      await addAssignment(caseId, user.id);
      toast.success(`${user.username} was assigned to the case.`);
      setSelectedUserId(null);
      setVersion((value) => value + 1);
    } catch (caught) {
      toast.error(describeError(caught));
    } finally {
      setAssigning(false);
    }
  }

  async function remove(assignment: Assignment) {
    setBusyUserId(assignment.user_id);
    try {
      await removeAssignment(caseId, assignment.user_id);
      toast.success(`${assignment.username} was removed from the case.`);
      setVersion((value) => value + 1);
    } catch (caught) {
      toast.error(describeError(caught));
    } finally {
      setBusyUserId(null);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Assignments</CardTitle>
        <CardDescription>Investigators and reviewers who work on this case.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {items === null && error === null && <p className="text-sm text-muted-foreground">Loading assignments…</p>}
        {items !== null && items.length === 0 && (
          <p className="text-sm text-muted-foreground">No one is assigned to this case yet.</p>
        )}
        {items !== null && items.length > 0 && (
          <div className="overflow-hidden rounded-md border">
            <Table>
              <TableHeader className="bg-muted">
                <TableRow>
                  <TableHead>User</TableHead>
                  <TableHead>Role</TableHead>
                  <TableHead>Assigned</TableHead>
                  <TableHead>Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {items.map((assignment) => (
                  <TableRow key={assignment.id}>
                    <TableCell>{assignment.username}</TableCell>
                    <TableCell>
                      <Badge variant="secondary">{humanize(assignment.role)}</Badge>
                    </TableCell>
                    <TableCell>
                      <Timestamp value={assignment.assigned_at} />
                    </TableCell>
                    <TableCell>
                      <DisabledHint
                        reason={
                          closed
                            ? "The case is closed. Assignments cannot be changed."
                            : !canAssign
                              ? "Your account cannot change assignments."
                              : undefined
                        }
                      >
                        <Button
                          size="sm"
                          variant="outline"
                          disabled={closed || !canAssign || busyUserId === assignment.user_id}
                          aria-label={`Remove ${assignment.username} from the case`}
                          onClick={() => void remove(assignment)}
                        >
                          Remove
                        </Button>
                      </DisabledHint>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
        {offerPicker && (
          <div className="flex flex-col gap-3 border-t pt-4">
            {pickerError && (
              <Alert variant="destructive">
                <AlertDescription>{pickerError}</AlertDescription>
              </Alert>
            )}
            {candidates === null && pickerError === null && (
              <p className="text-sm text-muted-foreground">Loading users…</p>
            )}
            {candidates !== null && available.length === 0 && (
              <p className="text-sm text-muted-foreground">No other user can be assigned to this case.</p>
            )}
            {available.length > 0 && (
              <div className="flex flex-wrap items-end gap-2">
                <Field className="min-w-64 flex-1">
                  <FieldLabel htmlFor="assign-user">
                    Add a user <span className="font-normal text-muted-foreground">(required)</span>
                  </FieldLabel>
                  <Select
                    value={selectedUserId}
                    onValueChange={(value) => {
                      if (value) setSelectedUserId(value);
                    }}
                  >
                    <SelectTrigger id="assign-user" className="w-full">
                      <SelectValue>
                        {(value: string | null) => {
                          const picked = available.find((candidate) => candidate.id === value);
                          return picked ? userLabel(picked) : "Choose a user";
                        }}
                      </SelectValue>
                    </SelectTrigger>
                    <SelectContent>
                      {available.map((user) => (
                        <SelectItem key={user.id} value={user.id}>
                          {userLabel(user)}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </Field>
                <Button onClick={() => void assign()} disabled={selectedUserId === null || assigning}>
                  Assign
                </Button>
              </div>
            )}
          </div>
        )}
      </CardContent>
    </Card>
  );
}
