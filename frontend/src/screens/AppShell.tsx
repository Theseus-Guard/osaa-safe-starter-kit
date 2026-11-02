import { LogOut, Menu } from "lucide-react";
import { ReactNode, useState } from "react";
import { Me } from "../api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { SystemClock } from "@/components/system-clock";
import { cn } from "@/lib/utils";
import { canReadCases } from "../permissions";
import { hashFor, Page, PAGE_INFO, PAGES, pageInNavigation } from "../routes";
import { isThemePreference, setPreference, ThemePreference, useThemePreference } from "../theme";
import { CaseSearch } from "./cases/CaseSearch";

interface Props {
  profile: Me;
  page: Page;
  onLogout: () => void;
  children: ReactNode;
  // Overrides the page title and description, for example on a case detail page.
  heading?: { title: string; description: string };
}

export function AppShell({ profile, page, onLogout, children, heading }: Props) {
  const [menuOpen, setMenuOpen] = useState(false);
  const isAdmin = profile.role === "admin";
  const visiblePages = PAGES.filter((item) => pageInNavigation(item, isAdmin, profile.permissions));
  const info = heading ?? PAGE_INFO[page];
  const canSearchCases = canReadCases(profile.permissions);

  return (
    <div className="flex min-h-svh flex-col">
      {/* Mobile: the search wraps onto its own row under the brand and user menu. Desktop: one row, search centred. */}
      <header className="sticky top-0 z-20 flex flex-wrap items-center justify-between gap-x-3 gap-y-2 border-b bg-background px-4 py-2 md:h-14 md:flex-nowrap md:py-0">
        <div className="flex items-center gap-2 md:flex-1">
          {/* On md and up the sidebar is shown instead, so the trigger is hidden there. */}
          <Sheet open={menuOpen} onOpenChange={setMenuOpen}>
            <SheetTrigger render={<Button variant="outline" size="icon" className="md:hidden" aria-label="Open navigation" />}>
              <Menu />
            </SheetTrigger>
            <SheetContent side="left">
              <SheetHeader>
                <SheetTitle>Navigation</SheetTitle>
              </SheetHeader>
              <div className="flex flex-col gap-2 px-3 pb-4">
                <Navigation pages={visiblePages} current={page} onNavigate={() => setMenuOpen(false)} />
                <Button variant="outline" className="mt-4 justify-start gap-2" onClick={onLogout}>
                  <LogOut />
                  Log out
                </Button>
              </div>
            </SheetContent>
          </Sheet>
          <span className="font-semibold tracking-tight">SAFE-Starter-Kit</span>
        </div>
        {canSearchCases && (
          <div className="order-last w-full md:order-none md:w-auto md:max-w-[38rem] md:flex-1">
            <CaseSearch />
          </div>
        )}
        <div className="flex min-w-0 items-center justify-end gap-3 md:flex-1">
          {/* The clock is hidden on phones, where the header has no room for it next to the user menu. */}
          <div className="hidden sm:block">
            <SystemClock />
          </div>
          <UserMenu profile={profile} onLogout={onLogout} />
        </div>
      </header>
      <div className="flex flex-1">
        {/* Pinned under the header (h-14 on md and up). Log out sits right under the navigation. */}
        <aside className="hidden w-56 shrink-0 flex-col border-r p-3 md:sticky md:top-14 md:flex md:h-[calc(100svh-3.5rem)] md:self-start">
          <Navigation pages={visiblePages} current={page} />
          <Button variant="outline" className="mt-2 justify-start gap-2" onClick={onLogout}>
            <LogOut />
            Log out
          </Button>
        </aside>
        <main id="content" className="min-w-0 flex-1 p-4 md:p-6">
          <div className="flex w-full flex-col gap-6">
            <div className="flex flex-col gap-1">
              <h1 className="text-2xl font-semibold tracking-tight">{info.title}</h1>
              <p className="text-sm text-muted-foreground">{info.description}</p>
            </div>
            {children}
          </div>
        </main>
      </div>
    </div>
  );
}

interface NavigationProps {
  pages: Page[];
  current: Page;
  onNavigate?: () => void;
}

function Navigation({ pages, current, onNavigate }: NavigationProps) {
  return (
    <nav aria-label="Main" className="flex flex-col gap-1">
      {pages.map((item) => {
        const active = item === current;
        return (
          <a
            key={item}
            href={hashFor(item)}
            aria-current={active ? "page" : undefined}
            onClick={onNavigate}
            className={cn(
              "rounded-md px-3 py-2 text-sm transition-colors outline-none focus-visible:ring-3 focus-visible:ring-ring",
              active
                ? "bg-accent font-medium text-accent-foreground"
                : "text-muted-foreground hover:bg-muted hover:text-foreground",
            )}
          >
            {PAGE_INFO[item].label}
          </a>
        );
      })}
    </nav>
  );
}

interface UserMenuProps {
  profile: Me;
  onLogout: () => void;
}

const APPEARANCE_OPTIONS: { value: ThemePreference; label: string }[] = [
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
  { value: "system", label: "System" },
];

// "First Last (username)" when a name is set, otherwise just the username.
function displayName(profile: { username: string; first_name?: string | null; last_name?: string | null }): string {
  const fullName = [profile.first_name, profile.last_name].filter(Boolean).join(" ");
  return fullName ? `${fullName} (${profile.username})` : profile.username;
}

function UserMenu({ profile, onLogout }: UserMenuProps) {
  const preference = useThemePreference();
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button variant="ghost" className="max-w-full gap-2" />}>
        <span className="max-w-32 truncate text-sm sm:max-w-64">{displayName(profile)}</span>
        <Badge variant="secondary">{profile.role}</Badge>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        <DropdownMenuGroup>
          <DropdownMenuLabel>Signed in as {displayName(profile)}</DropdownMenuLabel>
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuGroup>
          <DropdownMenuLabel>Appearance</DropdownMenuLabel>
          <DropdownMenuRadioGroup
            value={preference}
            onValueChange={(value) => {
              if (isThemePreference(value)) setPreference(value);
            }}
          >
            {APPEARANCE_OPTIONS.map((option) => (
              <DropdownMenuRadioItem key={option.value} value={option.value}>
                {option.label}
              </DropdownMenuRadioItem>
            ))}
          </DropdownMenuRadioGroup>
        </DropdownMenuGroup>
        <DropdownMenuSeparator />
        <DropdownMenuItem onClick={onLogout}>
          <LogOut />
          Log out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
