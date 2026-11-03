import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface Option<T extends string | number> {
  value: T;
  label: string;
}

interface SegmentedControlProps<T extends string | number> {
  // Names the group for assistive technology, for example "Date range".
  label: string;
  options: readonly Option<T>[];
  value: T;
  onChange: (value: T) => void;
}

// A row of mutually exclusive choices. The chosen option is pressed (aria-pressed) and shown with a raised surface.
export function SegmentedControl<T extends string | number>({ label, options, value, onChange }: SegmentedControlProps<T>) {
  return (
    <div role="group" aria-label={label} className="inline-flex gap-0.5 rounded-lg bg-muted p-0.5">
      {options.map((option) => {
        const active = option.value === value;
        return (
          <Button
            key={String(option.value)}
            type="button"
            size="sm"
            variant="ghost"
            aria-pressed={active}
            onClick={() => onChange(option.value)}
            className={cn(
              "min-w-16 hover:bg-background/70",
              active && "bg-background text-foreground shadow-sm hover:bg-background",
              !active && "text-muted-foreground",
            )}
          >
            {option.label}
          </Button>
        );
      })}
    </div>
  );
}
