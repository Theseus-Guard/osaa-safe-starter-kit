import { TriangleAlertIcon } from "lucide-react";
import { ChangeEvent, FormEvent, useEffect, useState } from "react";
import { toast } from "sonner";
import { getProfile, updateProfile, UserProfile } from "../api";
import { describeError } from "../messages";
import { changedProfileFields, EMAIL_REQUIRED_MESSAGE, profileToFields, ProfileFields } from "../profile";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";

// The caller's own contact details. Shown on the Account page above the password form.
export function ProfileCard() {
  const [saved, setSaved] = useState<UserProfile | null>(null);
  const [values, setValues] = useState<ProfileFields | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    let stale = false;
    getProfile()
      .then((profile) => {
        if (stale) return;
        setSaved(profile);
        setValues(profileToFields(profile));
        setLoadError(null);
      })
      .catch((caught: unknown) => {
        if (!stale) setLoadError(describeError(caught));
      });
    return () => {
      stale = true;
    };
  }, [reloadToken]);

  if (loadError !== null) {
    return (
      <Alert variant="destructive">
        <AlertDescription>{loadError}</AlertDescription>
      </Alert>
    );
  }
  if (saved === null || values === null) {
    return <p className="text-sm text-muted-foreground">Loading profile…</p>;
  }

  const initial = profileToFields(saved);
  const dirty = Object.keys(changedProfileFields(initial, values)).length > 0;
  const emailMissing = saved.email === null || saved.email.trim() === "";

  function setField(key: keyof ProfileFields) {
    return (event: ChangeEvent<HTMLInputElement>) => setValues((current) => (current ? { ...current, [key]: event.target.value } : current));
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    if (saved === null || values === null) return;
    setError(null);
    if (values.email.trim() === "") {
      setError(EMAIL_REQUIRED_MESSAGE);
      return;
    }
    setBusy(true);
    try {
      await updateProfile(changedProfileFields(initial, values));
      // Reload from the server so the form shows what was actually stored.
      setReloadToken((token) => token + 1);
      toast.success("Profile saved.");
    } catch (caught) {
      setError(describeError(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      {emailMissing && (
        <Alert>
          <TriangleAlertIcon />
          <AlertTitle>Add your email address</AlertTitle>
          <AlertDescription>Add an email address so that you can be contacted and sign in with it later.</AlertDescription>
        </Alert>
      )}
      <Card>
        <CardHeader>
          <CardTitle>Profile</CardTitle>
          <CardDescription>Your name, title and contact email.</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={handleSubmit} className="flex flex-col gap-4">
            <FieldGroup className="grid gap-4 sm:grid-cols-2">
              <Field>
                <FieldLabel htmlFor="profile-first-name">First name</FieldLabel>
                <Input id="profile-first-name" value={values.first_name} onChange={setField("first_name")} autoComplete="given-name" />
              </Field>
              <Field>
                <FieldLabel htmlFor="profile-last-name">Last name</FieldLabel>
                <Input id="profile-last-name" value={values.last_name} onChange={setField("last_name")} autoComplete="family-name" />
              </Field>
              <Field className="sm:col-span-2">
                <FieldLabel htmlFor="profile-title">Title</FieldLabel>
                <Input id="profile-title" value={values.title} onChange={setField("title")} autoComplete="organization-title" />
              </Field>
              <Field className="sm:col-span-2">
                <FieldLabel htmlFor="profile-email">
                  Email <span className="font-normal text-muted-foreground">(required)</span>
                </FieldLabel>
                <Input
                  id="profile-email"
                  type="email"
                  value={values.email}
                  onChange={setField("email")}
                  autoComplete="email"
                  required
                />
              </Field>
            </FieldGroup>
            {error && (
              <Alert variant="destructive">
                <AlertDescription>{error}</AlertDescription>
              </Alert>
            )}
            <Button type="submit" className="self-start" disabled={!dirty || busy}>
              {busy ? "Saving…" : "Save profile"}
            </Button>
          </form>
        </CardContent>
      </Card>
    </>
  );
}
