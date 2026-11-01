import { useState } from "react";
import { toast } from "sonner";
import { ApiError, updateSystemTimezone } from "../../api";
import { describeError } from "../../messages";
import { useSystemTimezone } from "../../system-timezone";
import { formatUtcPlain, offsetLabel } from "../../time";
import { TimezonePicker } from "@/components/timezone-picker";
import { Timestamp } from "@/components/timestamp";
import { Alert, AlertDescription } from "@/components/ui/alert";
import {
  AlertDialog,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Field, FieldLabel } from "@/components/ui/field";
import { Label } from "@/components/ui/label";

// The wording is fixed. Only the placeholders change.
function confirmationText(effectiveUtc: string, newZone: string): string {
  return (
    `Records already stored keep their UTC time. Only how times are displayed changes. ` +
    `From ${formatUtcPlain(effectiveUtc)} onward, times are shown in ${newZone}. ` +
    `Times recorded before this moment will still show their original UTC instant, so they can look out of order next to new records. ` +
    `This change is recorded in the audit log.`
  );
}

export function SettingsPage() {
  const { timezone, effectiveFrom, error: loadError, setCurrent, reload } = useSystemTimezone();
  const [selected, setSelected] = useState<string | null>(null);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [understood, setUnderstood] = useState(false);
  // The effective moment shown in the dialog. The server sets the real one when the change is saved.
  const [previewMoment, setPreviewMoment] = useState("");
  const [saving, setSaving] = useState(false);
  const [dialogError, setDialogError] = useState<string | null>(null);
  const [pageError, setPageError] = useState<string | null>(null);

  const changed = selected !== null && selected !== timezone;

  function openDialog() {
    setDialogError(null);
    setUnderstood(false);
    setPreviewMoment(new Date().toISOString());
    setDialogOpen(true);
  }

  async function confirmChange() {
    if (selected === null || !understood) return;
    setSaving(true);
    setDialogError(null);
    setPageError(null);
    try {
      // current is the value on screen. The backend rejects the save if someone else changed it meanwhile.
      const saved = await updateSystemTimezone({ timezone: selected, confirm: true, current: timezone });
      setCurrent(saved);
      setSelected(null);
      setDialogOpen(false);
      toast.success(`System timezone set to ${saved.timezone ?? selected}.`);
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 409) {
        setDialogOpen(false);
        setSelected(null);
        setPageError(describeError(caught));
        reload();
      } else {
        setDialogError(describeError(caught));
      }
    } finally {
      setSaving(false);
    }
  }

  return (
    <>
      {loadError && (
        <Alert variant="destructive">
          <AlertDescription>{loadError}</AlertDescription>
        </Alert>
      )}
      {pageError && (
        <Alert variant="destructive">
          <AlertDescription>{pageError}</AlertDescription>
        </Alert>
      )}

      <Card>
        <CardHeader>
          <CardTitle>Current timezone</CardTitle>
          <CardDescription>Times in this installation are displayed in this timezone. Stored values are always UTC.</CardDescription>
        </CardHeader>
        <CardContent>
          <dl className="grid gap-4 sm:grid-cols-3">
            <div className="grid gap-1">
              <dt className="font-medium">Timezone</dt>
              <dd className="text-sm text-muted-foreground">{timezone ?? "Not set"}</dd>
            </div>
            <div className="grid gap-1">
              <dt className="font-medium">UTC offset</dt>
              <dd className="text-sm text-muted-foreground">{timezone ? offsetLabel(timezone) : "Not set"}</dd>
            </div>
            <div className="grid gap-1">
              <dt className="font-medium">Effective from</dt>
              <dd className="text-sm text-muted-foreground">
                {effectiveFrom && timezone ? (
                  <>
                    <Timestamp value={effectiveFrom} />
                    <span className="block text-xs">UTC: {formatUtcPlain(effectiveFrom)}</span>
                  </>
                ) : (
                  "Not set"
                )}
              </dd>
            </div>
          </dl>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Change timezone</CardTitle>
          <CardDescription>
            Changing the timezone only changes how times are displayed. Stored times are not modified. Every change is recorded in the
            audit log.
          </CardDescription>
        </CardHeader>
        <CardContent className="flex flex-col gap-4">
          <Field>
            <FieldLabel htmlFor="new-timezone">New timezone</FieldLabel>
            <TimezonePicker id="new-timezone" value={selected ?? timezone} onChange={setSelected} disabled={saving} />
          </Field>
          <Button className="self-start" disabled={!changed} onClick={openDialog}>
            Review change
          </Button>
        </CardContent>
      </Card>

      <AlertDialog
        open={dialogOpen}
        onOpenChange={(open) => {
          if (!saving) setDialogOpen(open);
        }}
      >
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>Change the system timezone?</AlertDialogTitle>
            <AlertDialogDescription>{confirmationText(previewMoment, selected ?? "")}</AlertDialogDescription>
          </AlertDialogHeader>
          <div className="flex items-center gap-2">
            <Checkbox id="understood" checked={understood} onCheckedChange={(checked) => setUnderstood(checked)} />
            <Label htmlFor="understood">I understand</Label>
          </div>
          {dialogError && (
            <Alert variant="destructive">
              <AlertDescription>{dialogError}</AlertDescription>
            </Alert>
          )}
          <AlertDialogFooter>
            <AlertDialogCancel disabled={saving}>Cancel</AlertDialogCancel>
            <Button disabled={!understood || saving} onClick={() => void confirmChange()}>
              {saving ? "Saving…" : "Confirm change"}
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </>
  );
}
