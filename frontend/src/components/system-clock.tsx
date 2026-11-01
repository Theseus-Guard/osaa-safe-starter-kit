import { useEffect, useState } from "react";
import { useSystemTimezone } from "@/system-timezone";
import { formatClock } from "@/time";

// Live clock in the system timezone. Hidden until that timezone is known, so it never shows a guessed value.
export function SystemClock() {
  const { timezone } = useSystemTimezone();
  const [now, setNow] = useState(() => new Date());

  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  if (timezone === null) return null;
  const iso = now.toISOString();
  return (
    <time dateTime={iso} title={iso} className="whitespace-nowrap font-mono text-sm tabular-nums text-muted-foreground">
      <span className="hidden md:inline">System Time: </span>
      {formatClock(now, timezone)}
    </time>
  );
}
