import { FormEvent, useState } from "react";
import { toast } from "sonner";
import { CaseDetail, DeadlineStatus, DeadlineTask, updateDeadline } from "../../api";
import { describeError } from "../../messages";
import { humanize } from "./case-format";
import { Timestamp } from "@/components/timestamp";
import { DisabledHint } from "@/components/disabled-hint";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";

interface Props {
  caseId: string;
  deadlines: DeadlineTask[];
  closed: boolean;
  // The caller has cases:read_all or cases:read_assigned. Without it the status actions are locked.
  canUpdate: boolean;
  // The caller has cases:triage. Without it "Not applicable" is locked.
  canMarkNotApplicable: boolean;
  onChanged: (detail: CaseDetail) => void;
}

const CLOSED_REASON = "The case is closed. Deadlines cannot be changed.";
const NOT_STORED_REASON = "This deadline is not stored yet and cannot be changed.";
const NO_UPDATE_REASON = "Your account cannot change deadlines.";
const NOT_TRIAGER_REASON = "Only a triager can mark a deadline not applicable.";

export function CaseDeadlines({ caseId, deadlines, closed, canUpdate, canMarkNotApplicable, onChanged }: Props) {
  const [busyTaskId, setBusyTaskId] = useState<string | null>(null);
  // Only one row at a time asks for a not-applicable reason.
  const [reasonFor, setReasonFor] = useState<string | null>(null);
  const [reason, setReason] = useState("");

  async function change(task: DeadlineTask, status: DeadlineStatus, why?: string) {
    if (task.id === null) return;
    setBusyTaskId(task.id);
    try {
      const updated = await updateDeadline(caseId, task.id, status, why);
      setReasonFor(null);
      setReason("");
      toast.success(`${humanize(task.kind)} is now ${humanize(status).toLowerCase()}.`);
      onChanged(updated);
    } catch (caught) {
      toast.error(describeError(caught));
    } finally {
      setBusyTaskId(null);
    }
  }

  function submitReason(event: FormEvent, task: DeadlineTask) {
    event.preventDefault();
    const trimmed = reason.trim();
    if (trimmed.length === 0) {
      toast.error("Enter the reason the deadline does not apply.");
      return;
    }
    void change(task, "not_applicable", trimmed);
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Deadline tasks</CardTitle>
        <CardDescription>Due times are shown in the system timezone.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {deadlines.length === 0 ? (
          <p className="text-sm text-muted-foreground">No deadline tasks.</p>
        ) : (
          <div className="overflow-hidden rounded-md border">
            <Table>
              <TableHeader className="bg-muted">
                <TableRow>
                  <TableHead>Task</TableHead>
                  <TableHead>Due</TableHead>
                  <TableHead>Status</TableHead>
                  <TableHead>Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {deadlines.map((task) => {
                  const lockReason = closed
                    ? CLOSED_REASON
                    : task.id === null
                      ? NOT_STORED_REASON
                      : !canUpdate
                        ? NO_UPDATE_REASON
                        : undefined;
                  const notApplicableLock = lockReason ?? (canMarkNotApplicable ? undefined : NOT_TRIAGER_REASON);
                  const busy = busyTaskId === task.id;
                  return (
                    <TableRow key={task.id ?? `${task.kind}-${task.due_at}`}>
                      <TableCell>{humanize(task.kind)}</TableCell>
                      <TableCell>
                        <Timestamp value={task.due_at} />
                      </TableCell>
                      <TableCell>
                        <Badge variant="secondary">{humanize(task.status)}</Badge>
                        {task.not_applicable_reason && (
                          <p className="mt-1 text-xs text-muted-foreground">Reason: {task.not_applicable_reason}</p>
                        )}
                      </TableCell>
                      <TableCell className="whitespace-normal">
                        {reasonFor !== null && reasonFor === task.id ? (
                          <form className="flex flex-col gap-2" onSubmit={(event) => submitReason(event, task)}>
                            <Textarea
                              aria-label={`Reason ${humanize(task.kind)} does not apply`}
                              value={reason}
                              maxLength={1000}
                              onChange={(event) => setReason(event.target.value)}
                              rows={2}
                            />
                            <div className="flex gap-2">
                              <Button type="submit" size="sm" disabled={busy}>
                                Confirm
                              </Button>
                              <Button
                                type="button"
                                size="sm"
                                variant="outline"
                                onClick={() => {
                                  setReasonFor(null);
                                  setReason("");
                                }}
                              >
                                Cancel
                              </Button>
                            </div>
                          </form>
                        ) : (
                          <div className="flex flex-wrap gap-2">
                            {task.status !== "done" && (
                              <RowAction reason={lockReason} disabled={busy} onClick={() => void change(task, "done")}>
                                Mark done
                              </RowAction>
                            )}
                            {task.status !== "open" && (
                              <RowAction reason={lockReason} disabled={busy} onClick={() => void change(task, "open")}>
                                Set open
                              </RowAction>
                            )}
                            {task.status !== "not_applicable" && (
                              <RowAction
                                reason={notApplicableLock}
                                disabled={busy}
                                onClick={() => {
                                  setReason("");
                                  setReasonFor(task.id);
                                }}
                              >
                                Not applicable
                              </RowAction>
                            )}
                          </div>
                        )}
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}

interface RowActionProps {
  reason?: string;
  disabled: boolean;
  onClick: () => void;
  children: string;
}

function RowAction({ reason, disabled, onClick, children }: RowActionProps) {
  return (
    <DisabledHint reason={reason}>
      <Button size="sm" variant="outline" disabled={disabled || reason !== undefined} onClick={onClick}>
        {children}
      </Button>
    </DisabledHint>
  );
}
