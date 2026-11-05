import { FormEvent, useState } from "react";
import { toast } from "sonner";
import { ApiError, CaseDetail, updateCaseTitle } from "../../api";
import { describeError } from "../../messages";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";

interface Props {
  caseId: string;
  // The title and revision the case screen holds. The save sends the revision as If-Match.
  title: string;
  revision: number;
  // The case is closed. The title cannot change, so the edit button is not offered.
  closed: boolean;
  // The caller has cases:triage. Without it the title is shown and cannot be edited.
  canWrite: boolean;
  // Called with the saved case, so the case screen shows the new title and revision at once.
  onSaved: (updated: CaseDetail) => void;
  // Called when the case changed on the server after the screen read it. The screen reloads the case.
  onReload: () => void;
}

// The same limit as the API and case create: 1 to 200 characters, not only whitespace.
const TITLE_LIMIT = 200;
const PRECONDITION_FAILED = 412;

export function CaseTitleEdit({ caseId, title, revision, closed, canWrite, onSaved, onReload }: Props) {
  const [editing, setEditing] = useState(false);
  const canEdit = canWrite && !closed;

  if (!canEdit) return null;

  return (
    <>
      <Button variant="outline" size="sm" onClick={() => setEditing(true)}>
        Edit title
      </Button>
      <Dialog open={editing} onOpenChange={setEditing}>
        <DialogContent className="max-h-[90vh] overflow-y-auto">
          {/* Mounted only while the dialog is open, so every opening starts from the revision the screen holds. */}
          <EditTitleForm
            caseId={caseId}
            title={title}
            revision={revision}
            onSaved={(updated) => {
              setEditing(false);
              toast.success("Case title saved.");
              onSaved(updated);
            }}
            onReload={() => {
              setEditing(false);
              onReload();
            }}
          />
        </DialogContent>
      </Dialog>
    </>
  );
}

interface FormProps {
  caseId: string;
  title: string;
  revision: number;
  onSaved: (updated: CaseDetail) => void;
  onReload: () => void;
}

function EditTitleForm({ caseId, title, revision, onSaved, onReload }: FormProps) {
  const [value, setValue] = useState(title);
  const [error, setError] = useState<string | null>(null);
  // Set when the server holds a newer revision. Nothing was saved, so the form offers a reload instead of a save.
  const [stale, setStale] = useState(false);
  const [busy, setBusy] = useState(false);

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    const next = value.trim();
    // Counted in code points, as the API counts the characters.
    if (next === "" || Array.from(next).length > TITLE_LIMIT) {
      setError(`The title must be 1 to ${TITLE_LIMIT} characters and not only spaces.`);
      return;
    }
    setError(null);
    setBusy(true);
    try {
      onSaved(await updateCaseTitle(caseId, next, revision));
    } catch (caught) {
      setBusy(false);
      if (caught instanceof ApiError && caught.status === PRECONDITION_FAILED) {
        setStale(true);
      } else {
        setError(describeError(caught));
      }
    }
  }

  return (
    <>
      <DialogHeader>
        <DialogTitle>Edit case title</DialogTitle>
        <DialogDescription>Only the title changes. The other details of the case stay as they are.</DialogDescription>
      </DialogHeader>
      {stale && (
        <Alert variant="destructive">
          <AlertDescription>
            The title changed after this case was loaded, so nothing was saved. Reload the case to see the current title,
            then edit it again.
          </AlertDescription>
        </Alert>
      )}
      {error && (
        <Alert variant="destructive">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      {stale ? (
        <DialogFooter>
          <Button type="button" onClick={onReload}>
            Reload case
          </Button>
        </DialogFooter>
      ) : (
        <form onSubmit={handleSubmit} className="flex flex-col gap-4">
          <FieldGroup>
            <Field>
              <FieldLabel htmlFor="case-title-input">Case title</FieldLabel>
              <Input
                id="case-title-input"
                value={value}
                onChange={(event) => setValue(event.target.value)}
                aria-invalid={error !== null ? true : undefined}
                autoComplete="off"
              />
            </Field>
          </FieldGroup>
          <DialogFooter>
            <Button type="submit" disabled={busy}>
              {busy ? "Saving…" : "Save title"}
            </Button>
          </DialogFooter>
        </form>
      )}
    </>
  );
}
