import { Combobox, ComboboxContent, ComboboxEmpty, ComboboxInput, ComboboxItem, ComboboxList } from "@/components/ui/combobox";
import { timeZoneOptions } from "@/time";

const ZONES = timeZoneOptions();

interface Props {
  id: string;
  value: string | null;
  onChange: (zone: string) => void;
  disabled?: boolean;
}

// Searchable picker over the runtime's list of IANA timezones.
export function TimezonePicker({ id, value, onChange, disabled = false }: Props) {
  return (
    <Combobox
      items={ZONES}
      value={value}
      disabled={disabled}
      onValueChange={(next) => {
        if (next) onChange(next);
      }}
    >
      <ComboboxInput id={id} placeholder="Search timezones, for example Europe/Berlin" disabled={disabled} className="w-full" />
      <ComboboxContent>
        <ComboboxEmpty>No timezone matches.</ComboboxEmpty>
        <ComboboxList>
          {(zone: string) => (
            <ComboboxItem key={zone} value={zone}>
              {zone}
            </ComboboxItem>
          )}
        </ComboboxList>
      </ComboboxContent>
    </Combobox>
  );
}
