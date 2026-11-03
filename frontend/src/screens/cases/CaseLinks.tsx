import { useEffect, useState } from "react";
import { toast } from "sonner";
import { CaseLink, addCaseLink, listCaseLinks, removeCaseLink } from "../../api";
import { describeError } from "../../messages";
import { DisabledHint } from "@/components/disabled-hint";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Timestamp } from "@/components/timestamp";

interface Props {
  caseId: string;
  closed: boolean;
  // The caller has cases:triage. Without it the add form and the remove buttons are not shown.
  canWrite: boolean;
}

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

export function CaseLinks({ caseId, closed, canWrite }: Props) {
  const [items, setItems] = useState<CaseLink[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Bumped after an add or a removal so the list reloads from the server.
  const [version, setVersion] = useState(0);
  const [linkedCaseId, setLinkedCaseId] = useState("");
  const [inputError, setInputError] = useState<string | null>(null);
  const [linking, setLinking] = useState(false);
  const [busyLinkedCaseId, setBusyLinkedCaseId] = useState<string | null>(null);
  // The add form and the remove buttons are offered only to a caller with cases:triage on an open case.
  const offerForm = canWrite && !closed;

  useEffect(() => {
    let stale = false;
    setError(null);
    listCaseLinks(caseId)
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

  async function link() {
    const value = linkedCaseId.trim();
    if (!UUID_PATTERN.test(value)) {
      setInputError("Enter the case ID as a UUID, for example 3f2a9c1e-4b7d-4e8a-9c2f-1a2b3c4d5e6f.");
      return;
    }
    setInputError(null);
    setLinking(true);
    try {
      await addCaseLink(caseId, value);
      toast.success("Cases linked. Linking changes no task, deadline or status.");
      setLinkedCaseId("");
      setVersion((count) => count + 1);
    } catch (caught) {
      toast.error(describeError(caught));
    } finally {
      setLinking(false);
    }
  }

  async function unlink(item: CaseLink) {
    if (item.linked_case_id === null) return;
    const otherId = item.linked_case_id;
    setBusyLinkedCaseId(otherId);
    try {
      await removeCaseLink(caseId, otherId);
      toast.success("The link was removed. No task, deadline or status changed.");
      setVersion((count) => count + 1);
    } catch (caught) {
      toast.error(describeError(caught));
    } finally {
      setBusyLinkedCaseId(null);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Linked cases</CardTitle>
        <CardDescription>
          Other cases that concern the same incident. A link records the relation only; it changes no task, deadline or
          status.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {items === null && error === null && <p className="text-sm text-muted-foreground">Loading linked cases…</p>}
        {items !== null && items.length === 0 && (
          <p className="text-sm text-muted-foreground">No case is linked to this case yet.</p>
        )}
        {items !== null && items.length > 0 && (
          <div className="overflow-hidden rounded-md border">
            <Table>
              <TableHeader className="bg-muted">
                <TableRow>
                  <TableHead>Case</TableHead>
                  <TableHead>Case ID</TableHead>
                  <TableHead>Linked</TableHead>
                  <TableHead>Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {items.map((item, index) => (
                  <TableRow key={item.linked_case_id ?? `hidden-${index}`}>
                    <TableCell>
                      {item.linked_case_id === null ? "Not available to your account" : item.linked_case_title}
                    </TableCell>
                    <TableCell className="font-mono text-xs">{item.linked_case_id ?? "-"}</TableCell>
                    <TableCell>
                      <Timestamp value={item.created_at} />
                    </TableCell>
                    <TableCell>
                      {item.linked_case_id !== null && canWrite && (
                        <DisabledHint reason={closed ? "The case is closed. Links cannot be changed." : undefined}>
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={closed || busyLinkedCaseId === item.linked_case_id}
                            onClick={() => void unlink(item)}
                          >
                            Remove
                          </Button>
                        </DisabledHint>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
        {offerForm && (
          <form
            className="flex flex-col gap-3 border-t pt-4"
            onSubmit={(event) => {
              event.preventDefault();
              void link();
            }}
          >
            <Field>
              <FieldLabel htmlFor="link-case-id">
                Case ID to link <span className="font-normal text-muted-foreground">(required)</span>
              </FieldLabel>
              <div className="flex flex-wrap items-start gap-2">
                <Input
                  id="link-case-id"
                  className="min-w-64 flex-1 font-mono"
                  placeholder="Case ID (UUID)"
                  value={linkedCaseId}
                  aria-invalid={inputError !== null}
                  aria-describedby={inputError !== null ? "link-case-id-error" : undefined}
                  onChange={(event) => setLinkedCaseId(event.target.value)}
                />
                <Button type="submit" disabled={linking}>
                  Link case
                </Button>
              </div>
            </Field>
            {inputError && (
              <Alert variant="destructive" id="link-case-id-error">
                <AlertDescription>{inputError}</AlertDescription>
              </Alert>
            )}
          </form>
        )}
      </CardContent>
    </Card>
  );
}
