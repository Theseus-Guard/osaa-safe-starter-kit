import { Search } from "lucide-react";
import { KeyboardEvent, useEffect, useRef, useState } from "react";
import { CaseSummary, listCases } from "../../api";
import { describeError } from "../../messages";
import { hashFor } from "../../routes";
import { Badge } from "@/components/ui/badge";
import { Input } from "@/components/ui/input";

const DEBOUNCE_MS = 250;
const MAX_RESULTS = 8;

// Header search. Matches the case reference or the case UUID prefix. The backend limits results to the cases the caller can see.
export function CaseSearch() {
  const rootRef = useRef<HTMLDivElement>(null);
  const [query, setQuery] = useState("");
  // The trimmed query that was last sent after the debounce delay.
  const [term, setTerm] = useState("");
  // The term that items and error belong to. The list shows "Searching" until it matches the typed query.
  const [searchedTerm, setSearchedTerm] = useState("");
  const [items, setItems] = useState<CaseSummary[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);

  useEffect(() => {
    const timer = window.setTimeout(() => setTerm(query.trim()), DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [query]);

  useEffect(() => {
    if (term === "") {
      setItems([]);
      setSearchedTerm("");
      setError(null);
      setLoading(false);
      return;
    }
    let stale = false;
    setLoading(true);
    listCases(MAX_RESULTS, undefined, term)
      .then((page) => {
        if (stale) return;
        setItems(page.items);
        setError(null);
        setSearchedTerm(term);
      })
      .catch((caught: unknown) => {
        if (stale) return;
        setItems([]);
        setError(describeError(caught));
        setSearchedTerm(term);
      })
      .finally(() => {
        if (!stale) setLoading(false);
      });
    return () => {
      stale = true;
    };
  }, [term]);

  // Close the list when the pointer goes down outside the search, while the list is open.
  useEffect(() => {
    if (!open) return;
    function handlePointerDown(event: MouseEvent) {
      if (rootRef.current && !rootRef.current.contains(event.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", handlePointerDown);
    return () => document.removeEventListener("mousedown", handlePointerDown);
  }, [open]);

  const trimmed = query.trim();
  const pending = loading || trimmed !== searchedTerm;
  const dropdownOpen = open && trimmed !== "";
  const shown = pending || error !== null ? [] : items;

  function handleKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    if (event.key === "Escape") {
      setOpen(false);
      return;
    }
    // Enter opens the first result, but only once the results for the typed query have arrived.
    if (event.key === "Enter" && dropdownOpen && !pending && items.length > 0) {
      event.preventDefault();
      setOpen(false);
      window.location.hash = hashFor("cases", items[0].id);
    }
  }

  return (
    <div ref={rootRef} className="relative w-full">
      <Search className="pointer-events-none absolute top-1/2 left-2.5 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
      <Input
        type="search"
        aria-label="Search cases by ID"
        placeholder="Search cases by ID"
        autoComplete="off"
        className="pl-8"
        value={query}
        onChange={(event) => {
          setQuery(event.target.value);
          setOpen(true);
        }}
        onFocus={() => {
          if (query.trim() !== "") setOpen(true);
        }}
        onKeyDown={handleKeyDown}
      />
      {dropdownOpen && (
        <div className="absolute top-full right-0 left-0 z-30 mt-1 max-h-96 overflow-y-auto rounded-md border bg-popover p-1 text-popover-foreground shadow-md">
          {pending && <p className="px-2 py-1.5 text-sm text-muted-foreground">Searching…</p>}
          {!pending && error !== null && <p className="px-2 py-1.5 text-sm text-destructive">{error}</p>}
          {!pending && error === null && shown.length === 0 && (
            <p className="px-2 py-1.5 text-sm text-muted-foreground">No cases found</p>
          )}
          {!pending && shown.length > 0 && (
            <ul aria-label="Matching cases" className="flex flex-col">
              {shown.map((item) => (
                <li key={item.id}>
                  <a
                    href={hashFor("cases", item.id)}
                    onClick={() => setOpen(false)}
                    className="flex items-center gap-3 rounded-md px-2 py-1.5 text-sm outline-none hover:bg-muted focus-visible:ring-3 focus-visible:ring-ring"
                  >
                    <span className="shrink-0 font-mono font-medium">{item.reference}</span>
                    <span className="min-w-0 flex-1 truncate">{item.title}</span>
                    <Badge variant="secondary">{item.status}</Badge>
                  </a>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  );
}
