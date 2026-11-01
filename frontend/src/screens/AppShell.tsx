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
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetTrigger } from "@/components/ui/sheet";
import { cn } from "@/lib/utils";
import { hashFor, Page, PAGE_INFO, PAGES } from "../routes";

interface Props {
  profile: Me;
  page: Page;
  onLogout: () => void;
  children: ReactNode;
}

export function AppShell({ profile, page, onLogout, children }: Props) {
  const [menuOpen, setMenuOpen] = useState(false);
  const isAdmin = profile.role === "admin";
  const visiblePages = PAGES.filter((item) => isAdmin || !PAGE_INFO[item].adminOnly);
  const info = PAGE_INFO[page];

  return (
    <div className="flex min-h-svh flex-col">
      <header className="sticky top-0 z-20 flex h-14 items-center justify-between gap-3 border-b bg-background px-4">
        <div className="flex items-center gap-2">
          {/* On md and up the sidebar is shown instead, so the trigger is hidden there. */}
          <Sheet open={menuOpen} onOpenChange={setMenuOpen}>
            <SheetTrigger render={<Button variant="outline" size="icon" className="md:hidden" aria-label="Open navigation" />}>
              <Menu />
            </SheetTrigger>
            <SheetContent side="left">
              <SheetHeader>
                <SheetTitle>Navigation</SheetTitle>
              </SheetHeader>
              <div className="px-3 pb-4">
                <Navigation pages={visiblePages} current={page} onNavigate={() => setMenuOpen(false)} />
              </div>
            </SheetContent>
          </Sheet>
          <span className="font-semibold tracking-tight">SAFE-Starter-Kit</span>
        </div>
        <UserMenu profile={profile} onLogout={onLogout} />
      </header>
      <div className="flex flex-1">
        <aside className="hidden w-56 shrink-0 border-r p-3 md:block">
          <Navigation pages={visiblePages} current={page} />
        </aside>
        <main id="content" className="min-w-0 flex-1 p-4 md:p-6">
          <div className="mx-auto flex w-full max-w-5xl flex-col gap-6">
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
              "rounded-md px-3 py-2 text-sm transition-colors outline-none focus-visible:ring-3 focus-visible:ring-ring/50",
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

function UserMenu({ profile, onLogout }: UserMenuProps) {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger render={<Button variant="ghost" className="max-w-full gap-2" />}>
        <span className="max-w-40 truncate text-sm">{profile.username}</span>
        <Badge variant="secondary">{profile.role}</Badge>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-56">
        <DropdownMenuGroup>
          <DropdownMenuLabel>Signed in as {profile.username}</DropdownMenuLabel>
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
