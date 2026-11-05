import { useEffect, useState } from "react";
import { toast } from "sonner";
import {
  AddCaseLinkRequest,
  CaseLink,
  CaseLinkType,
  CaseSummary,
  addCaseLink,
  listCaseLinks,
  listCases,
  removeCaseLink,
} from "../../api";
import { describeError } from "../../messages";
import { DisabledHint } from "@/components/disabled-hint";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Field, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { Timestamp } from "@/components/timestamp";

interface Props {
  caseId: string;
  closed: boolean;
  // The caller has cases:triage. Without it the add form and the remove buttons are not shown.
  canWrite: boolean;
  // Called after a link is added or removed, so the case header and the activity history reload.
  onChanged?: () => void;
}

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const REASON_MAX_LENGTH = 500;

const LINK_TYPES: Record<CaseLinkType, { label: string; help: string }> = {
  related: {
    label: "Related",
    help: "Reference only. Neither case's status, tasks or deadlines change.",
  },
  duplicate: {
    label: "Duplicate",
    help: "The duplicate case is closed when you link. Linking is refused while the duplicate still has open work: an open deadline task, a review layer that is not set, or an external notice that is not finished. Its tasks are not moved to the main case.",
  },
  root_sub: {
    label: "Root and sub-case",
    help: "A user who can read the sub-case can read the root case through this link. Read only: no write access to the root.",
  },
};

// Which case is the main case of a duplicate, or the root case of a root and sub-case link.
type MainChoice = "this" | "linked";

type FormField = "linked" | "main" | "reason";

interface FormError {
  field: FormField;
  message: string;
}

// The summary of one link as a table cell. primary_side names the main or root case.
function typeSummary(item: CaseLink): string {
  const type = LINK_TYPES[item.link_type].label;
  if (item.primary_side === null) return type;
  const main = item.primary_side === "this" ? "this case" : "linked case";
  return item.link_type === "duplicate" ? `${type}, main case: ${main}` : `${type}, root: ${main}`;
}

export function CaseLinks({ caseId, closed, canWrite, onChanged }: Props) {
  const [items, setItems] = useState<CaseLink[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Bumped after an add or a removal so the list reloads from the server.
  const [version, setVersion] = useState(0);
  const [linkedCaseId, setLinkedCaseId] = useState("");
  // The case finder: search by case reference, then use a result to fill the case ID.
  const [findText, setFindText] = useState("");
  const [findResults, setFindResults] = useState<CaseSummary[] | null>(null);
  const [findError, setFindError] = useState<string | null>(null);
  const [chosenReference, setChosenReference] = useState<string | null>(null);
  const [linkType, setLinkType] = useState<CaseLinkType>("related");
  const [mainChoice, setMainChoice] = useState<MainChoice | "">("");
  const [reason, setReason] = useState("");
  const [shareVisibility, setShareVisibility] = useState(false);
  const [formError, setFormError] = useState<FormError | null>(null);
  const [linking, setLinking] = useState(false);
  const [busyLinkedCaseId, setBusyLinkedCaseId] = useState<string | null>(null);
  // The add form and the remove buttons are offered only to a caller with cases:triage on an open case.
  const offerForm = canWrite && !closed;
  const needsMain = linkType !== "related";

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

  async function find() {
    const text = findText.trim();
    if (text === "") return;
    setFindError(null);
    try {
      const page = await listCases(10, undefined, text);
      setFindResults(page.items.filter((item) => item.id !== caseId));
    } catch (caught: unknown) {
      setFindError(describeError(caught));
    }
  }

  function useCase(item: CaseSummary) {
    setLinkedCaseId(item.id);
    setChosenReference(item.reference);
    setFormError(null);
  }

  function validate(): { request: AddCaseLinkRequest } | { error: FormError } {
    const value = linkedCaseId.trim();
    if (!UUID_PATTERN.test(value)) {
      return {
        error: {
          field: "linked",
          message: "Enter the case ID as a UUID, for example 3f2a9c1e-4b7d-4e8a-9c2f-1a2b3c4d5e6f.",
        },
      };
    }
    const text = reason.trim();
    if (text === "") {
      return { error: { field: "reason", message: "Enter a short reason for the link." } };
    }
    if (text.length > REASON_MAX_LENGTH) {
      return { error: { field: "reason", message: `The reason can be at most ${REASON_MAX_LENGTH} characters.` } };
    }
    const request: AddCaseLinkRequest = {
      linked_case_id: value,
      link_type: linkType,
      reason: text,
      share_visibility: shareVisibility,
    };
    if (needsMain) {
      if (mainChoice === "") {
        return {
          error: {
            field: "main",
            message:
              linkType === "duplicate"
                ? "Choose which case is the main case. The other case is closed."
                : "Choose which case is the root case.",
          },
        };
      }
      request.primary_case_id = mainChoice === "this" ? caseId : value;
    }
    return { request };
  }

  async function link() {
    const checked = validate();
    if ("error" in checked) {
      setFormError(checked.error);
      return;
    }
    setFormError(null);
    setLinking(true);
    try {
      await addCaseLink(caseId, checked.request);
      toast.success(
        linkType === "duplicate"
          ? "Duplicate linked. The duplicate case is now closed. No task was moved to the main case."
          : "Cases linked. Linking changes no task, deadline or status.",
      );
      setLinkedCaseId("");
      setChosenReference(null);
      setFindResults(null);
      setFindText("");
      setReason("");
      setShareVisibility(false);
      setMainChoice("");
      setVersion((count) => count + 1);
      onChanged?.();
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
      onChanged?.();
    } catch (caught) {
      toast.error(describeError(caught));
    } finally {
      setBusyLinkedCaseId(null);
    }
  }

  const errorId = formError ? `link-form-error-${formError.field}` : undefined;
  const invalid = (field: FormField) => (formError?.field === field ? true : undefined);
  const describedBy = (field: FormField) => (formError?.field === field ? errorId : undefined);

  return (
    <Card>
      <CardHeader>
        <CardTitle>Linked cases</CardTitle>
        <CardDescription>
          Other cases that concern the same incident. A link records the relation and its type; a duplicate link also closes
          the duplicate case.
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
                  <TableHead>Type</TableHead>
                  <TableHead>Reason</TableHead>
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
                      {typeSummary(item)}
                      {item.primary_side !== null && (
                        <span className="block font-mono text-xs text-muted-foreground">
                          Main or root case ID: {item.primary_case_id ?? "not available to your account"}
                        </span>
                      )}
                      {item.share_visibility && (
                        <span className="block text-xs text-muted-foreground">Users of either case see both</span>
                      )}
                    </TableCell>
                    <TableCell>
                      {item.reason ?? (item.linked_case_id === null ? "Not available to your account" : "-")}
                    </TableCell>
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
              <FieldLabel htmlFor="link-case-find">
                Find the case by reference <span className="font-normal text-muted-foreground">(optional)</span>
              </FieldLabel>
              <div className="flex flex-wrap gap-2">
                <Input
                  id="link-case-find"
                  className="min-w-64 flex-1"
                  placeholder="Case reference, or part of it"
                  value={findText}
                  onChange={(event) => setFindText(event.target.value)}
                />
                <Button type="button" variant="outline" onClick={() => void find()} disabled={findText.trim() === ""}>
                  Find
                </Button>
              </div>
            </Field>
            {findError !== null && (
              <Alert variant="destructive">
                <AlertDescription>{findError}</AlertDescription>
              </Alert>
            )}
            {findResults !== null && (
              <div className="flex flex-col gap-2 text-sm">
                {findResults.length === 0 ? (
                  <p className="text-muted-foreground">No other visible case matches that reference.</p>
                ) : (
                  <ul className="flex flex-col gap-1">
                    {findResults.map((item) => (
                      <li key={item.id} className="flex flex-wrap items-center gap-2">
                        <span className="font-mono text-xs">{item.reference}</span>
                        <span className="flex-1">{item.title}</span>
                        <Badge variant="outline">{item.status}</Badge>
                        <Button type="button" variant="outline" size="sm" onClick={() => useCase(item)}>
                          Use this case
                        </Button>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
            <Field>
              <FieldLabel htmlFor="link-case-id">
                Case ID to link <span className="font-normal text-muted-foreground">(required)</span>
              </FieldLabel>
              <Input
                id="link-case-id"
                className="min-w-64 flex-1 font-mono"
                placeholder="Case ID (UUID)"
                value={linkedCaseId}
                aria-invalid={invalid("linked")}
                aria-describedby={describedBy("linked")}
                onChange={(event) => {
                  setLinkedCaseId(event.target.value);
                  setChosenReference(null);
                }}
              />
              {chosenReference !== null && (
                <span className="text-sm text-muted-foreground">Selected case: {chosenReference}</span>
              )}
            </Field>
            <Field>
              <FieldLabel htmlFor="link-type">
                Link type <span className="font-normal text-muted-foreground">(required)</span>
              </FieldLabel>
              <Select
                value={linkType}
                onValueChange={(value) => {
                  if (value) setLinkType(value as CaseLinkType);
                }}
              >
                <SelectTrigger id="link-type" className="w-full">
                  <SelectValue>{(value: string | null) => LINK_TYPES[(value ?? "related") as CaseLinkType].label}</SelectValue>
                </SelectTrigger>
                <SelectContent>
                  {(Object.keys(LINK_TYPES) as CaseLinkType[]).map((type) => (
                    <SelectItem key={type} value={type}>
                      {LINK_TYPES[type].label}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <p id="link-type-help" className="text-xs text-muted-foreground">
                {LINK_TYPES[linkType].help}
              </p>
            </Field>
            {needsMain && (
              <Field>
                <FieldLabel htmlFor="link-main">
                  {linkType === "duplicate" ? "Main case" : "Root case"}{" "}
                  <span className="font-normal text-muted-foreground">(required)</span>
                </FieldLabel>
                <Select
                  value={mainChoice}
                  onValueChange={(value) => {
                    if (value === "this" || value === "linked") setMainChoice(value);
                  }}
                >
                  <SelectTrigger id="link-main" className="w-full" aria-invalid={invalid("main")}>
                    <SelectValue>
                      {(value: string | null) =>
                        value === "this" ? "This case" : value === "linked" ? "The case ID above" : "Choose a case"
                      }
                    </SelectValue>
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="this">This case</SelectItem>
                    <SelectItem value="linked">The case ID above</SelectItem>
                  </SelectContent>
                </Select>
              </Field>
            )}
            <Field>
              <FieldLabel htmlFor="link-reason">
                Link reason <span className="font-normal text-muted-foreground">(required)</span>
              </FieldLabel>
              <Input
                id="link-reason"
                placeholder="Why these cases are linked"
                value={reason}
                aria-invalid={invalid("reason")}
                aria-describedby={describedBy("reason")}
                onChange={(event) => setReason(event.target.value)}
              />
            </Field>
            <div className="flex items-center gap-2">
              <Checkbox
                id="link-share"
                checked={shareVisibility}
                onCheckedChange={(checked) => setShareVisibility(checked === true)}
              />
              <FieldLabel htmlFor="link-share">
                Users who can see one of the two cases can see the other case too
              </FieldLabel>
            </div>
            <div>
              <Button type="submit" disabled={linking}>
                Link case
              </Button>
            </div>
            {formError && (
              <Alert variant="destructive" id={errorId}>
                <AlertDescription>{formError.message}</AlertDescription>
              </Alert>
            )}
          </form>
        )}
      </CardContent>
    </Card>
  );
}
