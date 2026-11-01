import { useEffect, useState } from "react";

// Pages live in the URL hash (for example #/users) so refresh and back work without a router library.
export const PAGES = ["overview", "users", "privacy", "roles", "settings", "account"] as const;

export type Page = (typeof PAGES)[number];

interface PageInfo {
  label: string;
  title: string;
  description: string;
  adminOnly: boolean;
}

export const PAGE_INFO: Record<Page, PageInfo> = {
  overview: {
    label: "Overview",
    title: "Overview",
    description: "Your account at a glance.",
    adminOnly: false,
  },
  users: {
    label: "Users",
    title: "Users",
    description: "Create accounts, change roles and status, and reset temporary passwords.",
    adminOnly: true,
  },
  privacy: {
    label: "Data privacy",
    title: "Data privacy",
    description: "Personal data inventory of this installation. Metadata only; row contents are never shown.",
    adminOnly: true,
  },
  roles: {
    label: "Roles and permissions",
    title: "Roles and permissions",
    description: "Review and edit the permissions of each role. Changes apply immediately to everyone with that role.",
    adminOnly: true,
  },
  settings: {
    label: "Settings",
    title: "Settings",
    description: "System timezone and how times are displayed.",
    adminOnly: true,
  },
  account: {
    label: "Account",
    title: "Account",
    description: "Change the password you use to sign in.",
    adminOnly: false,
  },
};

export function hashFor(page: Page): string {
  return `#/${page}`;
}

// Unknown hashes, and admin pages for non-admins, fall back to Overview.
export function pageFromHash(hash: string, isAdmin: boolean): Page {
  const name = hash.replace(/^#\/?/, "");
  const match = PAGES.find((candidate) => candidate === name);
  if (match === undefined) return "overview";
  if (PAGE_INFO[match].adminOnly && !isAdmin) return "overview";
  return match;
}

export function useHashPage(isAdmin: boolean): Page {
  const [page, setPage] = useState<Page>(() => pageFromHash(window.location.hash, isAdmin));

  useEffect(() => {
    function sync() {
      setPage(pageFromHash(window.location.hash, isAdmin));
    }
    sync();
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, [isAdmin]);

  useEffect(() => {
    // Rewrite unknown or missing hashes so the URL matches the page on screen. replaceState does not fire hashchange.
    if (window.location.hash !== hashFor(page)) window.history.replaceState(null, "", hashFor(page));
  }, [page]);

  return page;
}
