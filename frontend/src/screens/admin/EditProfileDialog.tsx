import { ChangeEvent, FormEvent, useState } from "react";
import { toast } from "sonner";
import { updateUserProfile, UserSummary } from "../../api";
import { describeError } from "../../messages";
import { changedProfileFields, EMAIL_REQUIRED_MESSAGE, profileToFields, ProfileFields } from "../../profile";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";

interface Props {
  // The user being edited, or null when the dialog is closed.
  user: UserSummary | null;
  onClose: () => void;
  onSaved: () => void;
}

export function EditProfileDialog({ user, onClose, onSaved }: Props) {
  return (
    <Dialog
      open={user !== null}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
    >
      <DialogContent>
        {user && <ProfileForm key={user.id} user={user} onClose={onClose} onSaved={onSaved} />}
      </DialogContent>
    </Dialog>
  );
}

function ProfileForm({ user, onClose, onSaved }: { user: UserSummary; onClose: () => void; onSaved: () => void }) {
  const initial = profileToFields(user);
  const [values, setValues] = useState<ProfileFields>(initial);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function setField(key: keyof ProfileFields) {
    return (event: ChangeEvent<HTMLInputElement>) => setValues((current) => ({ ...current, [key]: event.target.value }));
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (values.email.trim() === "") {
      setError(EMAIL_REQUIRED_MESSAGE);
      return;
    }
    const update = changedProfileFields(initial, values);
    if (Object.keys(update).length === 0) {
      onClose();
      return;
    }
    setBusy(true);
    try {
      await updateUserProfile(user.id, update);
      toast.success(`Profile of ${user.username} saved.`);
      onSaved();
    } catch (caught) {
      setError(describeError(caught));
      setBusy(false);
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-4">
      <DialogHeader>
        <DialogTitle>Edit profile</DialogTitle>
        <DialogDescription>Contact details for {user.username}.</DialogDescription>
      </DialogHeader>
      <FieldGroup className="grid gap-4 sm:grid-cols-2">
        <Field>
          <FieldLabel htmlFor="edit-profile-first-name">First name</FieldLabel>
          <Input id="edit-profile-first-name" value={values.first_name} onChange={setField("first_name")} />
        </Field>
        <Field>
          <FieldLabel htmlFor="edit-profile-last-name">Last name</FieldLabel>
          <Input id="edit-profile-last-name" value={values.last_name} onChange={setField("last_name")} />
        </Field>
        <Field className="sm:col-span-2">
          <FieldLabel htmlFor="edit-profile-title">Title</FieldLabel>
          <Input id="edit-profile-title" value={values.title} onChange={setField("title")} />
        </Field>
        <Field className="sm:col-span-2">
          <FieldLabel htmlFor="edit-profile-email">
            Email <span className="font-normal text-muted-foreground">(required)</span>
          </FieldLabel>
          <Input id="edit-profile-email" type="email" value={values.email} onChange={setField("email")} required />
        </Field>
      </FieldGroup>
      {error && (
        <Alert variant="destructive" id="edit-profile-error">
          <AlertDescription>{error}</AlertDescription>
        </Alert>
      )}
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onClose} disabled={busy}>
          Cancel
        </Button>
        <Button type="submit" disabled={busy}>
          {busy ? "Saving…" : "Save profile"}
        </Button>
      </DialogFooter>
    </form>
  );
}
