import { KeyboardEvent, ReactNode, useId, useRef } from "react";
import { cn } from "@/lib/utils";

export interface TabDefinition<T extends string> {
  id: T;
  label: string;
  content: ReactNode;
}

interface TabsProps<T extends string> {
  // Names the tab list for assistive technology, for example "Case sections".
  label: string;
  tabs: readonly TabDefinition<T>[];
  value: T;
  onChange: (id: T) => void;
}

// A tab bar with one panel per tab. Every panel stays mounted, so switching tabs keeps typed input and does not
// reload data. Only the selected panel is shown. Arrow keys, Home and End move between tabs.
export function Tabs<T extends string>({ label, tabs, value, onChange }: TabsProps<T>) {
  const baseId = useId();
  const buttons = useRef(new Map<T, HTMLButtonElement>());

  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    const last = tabs.length - 1;
    let target: number | null = null;
    if (event.key === "ArrowRight") target = index === last ? 0 : index + 1;
    else if (event.key === "ArrowLeft") target = index === 0 ? last : index - 1;
    else if (event.key === "Home") target = 0;
    else if (event.key === "End") target = last;
    if (target === null) return;
    event.preventDefault();
    const next = tabs[target];
    onChange(next.id);
    buttons.current.get(next.id)?.focus();
  }

  return (
    <div className="flex flex-col gap-4">
      <div role="tablist" aria-label={label} className="flex gap-1 overflow-x-auto border-b">
        {tabs.map((tab, index) => {
          const selected = tab.id === value;
          return (
            <button
              key={tab.id}
              ref={(node) => {
                if (node) buttons.current.set(tab.id, node);
                else buttons.current.delete(tab.id);
              }}
              type="button"
              role="tab"
              id={`${baseId}-tab-${tab.id}`}
              aria-selected={selected}
              aria-controls={`${baseId}-panel-${tab.id}`}
              tabIndex={selected ? 0 : -1}
              onClick={() => onChange(tab.id)}
              onKeyDown={(event) => handleKeyDown(event, index)}
              className={cn(
                "-mb-px shrink-0 rounded-t-md border-b-2 px-3 py-2 text-sm font-medium whitespace-nowrap transition-colors outline-none focus-visible:ring-3 focus-visible:ring-ring",
                selected
                  ? "border-foreground text-foreground"
                  : "border-transparent text-muted-foreground hover:border-border hover:text-foreground",
              )}
            >
              {tab.label}
            </button>
          );
        })}
      </div>
      {tabs.map((tab) => (
        <div
          key={tab.id}
          role="tabpanel"
          id={`${baseId}-panel-${tab.id}`}
          aria-labelledby={`${baseId}-tab-${tab.id}`}
          hidden={tab.id !== value}
          className="flex flex-col gap-4"
        >
          {tab.content}
        </div>
      ))}
    </div>
  );
}
