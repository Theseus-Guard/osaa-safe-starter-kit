import { useEffect, useState } from "react";

// Pages live in the URL hash (for example #/users or #/cases/{id}) so refresh and back work without a router library.
export const PAGES = ["overview", "cases", "users", "privacy", "roles", "settings", "account"] as const;

export type Page = (typeof PAGES)[number];

export interface Route {
  page: Page;
  // Set only on the case detail page, #/cases/{id}.
  caseId: string | null;
}

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
  cases: {
    label: "Cases",
    title: "Cases",
    description: "Cases you have opened. Deadlines are calculated from the awareness time.",
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

export function hashFor(page: Page, caseId: string | null = null): string {
  return caseId === null ? `#/${page}` : `#/${page}/${encodeURIComponent(caseId)}`;
}

// Unknown hashes, and admin pages for non-admins, fall back to Overview.
export function routeFromHash(hash: string, isAdmin: boolean): Route {
  const [name, id] = hash.replace(/^#\/?/, "").split("/");
  const match = PAGES.find((candidate) => candidate === name);
  if (match === undefined) return { page: "overview", caseId: null };
  if (PAGE_INFO[match].adminOnly && !isAdmin) return { page: "overview", caseId: null };
  return { page: match, caseId: match === "cases" && id ? decodeURIComponent(id) : null };
}

export function useHashRoute(isAdmin: boolean): Route {
  const [route, setRoute] = useState<Route>(() => routeFromHash(window.location.hash, isAdmin));

  useEffect(() => {
    function sync() {
      setRoute(routeFromHash(window.location.hash, isAdmin));
    }
    sync();
    window.addEventListener("hashchange", sync);
    return () => window.removeEventListener("hashchange", sync);
  }, [isAdmin]);

  useEffect(() => {
    // Rewrite unknown or missing hashes so the URL matches the page on screen. replaceState does not fire hashchange.
    const expected = hashFor(route.page, route.caseId);
    if (window.location.hash !== expected) window.history.replaceState(null, "", expected);
  }, [route.page, route.caseId]);

  return route;
}
