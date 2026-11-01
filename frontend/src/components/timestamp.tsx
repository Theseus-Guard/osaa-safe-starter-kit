import { useSystemTimezone } from "@/system-timezone";
import { DEFAULT_ZONE, formatInTimezone } from "@/time";

interface Props {
  // A UTC ISO string from the API.
  value: string;
}

// Shows a stored instant in the system timezone. The full UTC value is kept in the title and the datetime attribute.
export function Timestamp({ value }: Props) {
  const { timezone } = useSystemTimezone();
  return (
    <time dateTime={value} title={value}>
      {formatInTimezone(value, timezone ?? DEFAULT_ZONE)}
    </time>
  );
}
