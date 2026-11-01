import { useState } from "react";
import { getReporterContact } from "../../api";
import { describeError } from "../../messages";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";

interface Props {
  caseId: string;
}

// The contact is read only when the user asks for it. Each read is written to the audit log, so the section never loads on page load.
export function CaseReporterContact({ caseId }: Props) {
  const [busy, setBusy] = useState(false);
  // Undefined until the contact has been read. Null means no contact is recorded.
  const [contact, setContact] = useState<string | null | undefined>(undefined);
  const [error, setError] = useState<string | null>(null);

  async function reveal() {
    setBusy(true);
    setError(null);
    try {
      setContact(await getReporterContact(caseId));
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>Reporter contact</CardTitle>
        <CardDescription>The follow-up contact the reporter gave for this case.</CardDescription>
      </CardHeader>
      <CardContent className="flex flex-col gap-4">
        {error && (
          <Alert variant="destructive">
            <AlertDescription>{error}</AlertDescription>
          </Alert>
        )}
        {contact === undefined && (
          <div className="flex flex-col gap-2">
            <div>
              <Button variant="outline" disabled={busy} onClick={() => void reveal()}>
                Show reporter contact
              </Button>
            </div>
            <p className="text-sm text-muted-foreground">Reading the contact is recorded in the audit log.</p>
          </div>
        )}
        {contact === null && <p className="text-sm text-muted-foreground">No contact recorded</p>}
        {contact !== undefined && contact !== null && <p className="whitespace-pre-wrap text-sm">{contact}</p>}
      </CardContent>
    </Card>
  );
}
