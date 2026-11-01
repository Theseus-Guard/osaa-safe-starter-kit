import { useEffect, useState } from "react";
import { toast } from "sonner";
import { Assignment, listAssignments, removeAssignment } from "../../api";
import { describeError } from "../../messages";
import { humanize } from "./case-format";
import { Timestamp } from "@/components/timestamp";
import { DisabledHint } from "@/components/disabled-hint";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

interface Props {
  caseId: string;
  closed: boolean;
  // The caller has cases:assign. Without it removal is locked.
  canAssign: boolean;
}

export function CaseAssignments({ caseId, closed, canAssign }: Props) {
  const [items, setItems] = useState<Assignment[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busyUserId, setBusyUserId] = useState<string | null>(null);
  // Bumped after a removal so the list reloads from the server.
  const [version, setVersion] = useState(0);

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
      </CardContent>
    </Card>
  );
}
