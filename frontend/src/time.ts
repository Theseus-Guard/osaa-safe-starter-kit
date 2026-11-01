// Timestamps from the API are UTC ISO strings. They are stored and sorted as UTC; only the display follows the system timezone.

export const DEFAULT_ZONE = "UTC";

// Falls back to UTC for a name the runtime does not know, so a bad value cannot break the page.
function resolveZone(timezone: string): string {
  try {
    return new Intl.DateTimeFormat("en-US", { timeZone: timezone }).resolvedOptions().timeZone;
  } catch {
    return DEFAULT_ZONE;
  }
}

function pad(value: number): string {
  return String(value).padStart(2, "0");
}

// Offset of the zone from UTC at the given instant, in minutes.
function offsetMinutes(timezone: string, at: Date): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: resolveZone(timezone),
    hourCycle: "h23",
    year: "numeric",
    month: "numeric",
    day: "numeric",
    hour: "numeric",
    minute: "numeric",
    second: "numeric",
  }).formatToParts(at);
  const field = (type: Intl.DateTimeFormatPartTypes) => Number(parts.find((part) => part.type === type)?.value);
  const wallClockAsUtc = Date.UTC(
    field("year"),
    field("month") - 1,
    field("day"),
    field("hour"),
    field("minute"),
    field("second"),
  );
  const atWholeSecond = Math.floor(at.getTime() / 1000) * 1000;
  return Math.round((wallClockAsUtc - atWholeSecond) / 60_000);
}

// For example "UTC+03:00" or "UTC-05:00" at the given instant.
export function offsetLabel(timezone: string, at: Date = new Date()): string {
  const minutes = offsetMinutes(timezone, at);
  const sign = minutes < 0 ? "-" : "+";
  const absolute = Math.abs(minutes);
  return `UTC${sign}${pad(Math.floor(absolute / 60))}:${pad(absolute % 60)}`;
}

// For example "Nov 1, 2026, 09:05 UTC+03:00". The offset is always shown so the zone is never ambiguous.
export function formatInTimezone(isoUtc: string, timezone: string): string {
  const date = new Date(isoUtc);
  if (Number.isNaN(date.getTime())) return isoUtc;
  const zone = resolveZone(timezone);
  const wallClock = new Intl.DateTimeFormat("en-US", {
    timeZone: zone,
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).format(date);
  return `${wallClock} ${offsetLabel(zone, date)}`;
}

// For example "09:05:42 (UTC+03:00)". Used by the live clock in the top bar.
export function formatClock(at: Date, timezone: string): string {
  const zone = resolveZone(timezone);
  const clock = new Intl.DateTimeFormat("en-US", {
    timeZone: zone,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).format(at);
  return `${clock} (${offsetLabel(zone, at)})`;
}

// For example "2026-11-01 06:05 UTC". Used where the UTC reference must be explicit.
export function formatUtcPlain(isoUtc: string): string {
  const date = new Date(isoUtc);
  if (Number.isNaN(date.getTime())) return isoUtc;
  return `${date.toISOString().slice(0, 16).replace("T", " ")} UTC`;
}

// Wall-clock date ("YYYY-MM-DD") and time ("HH:MM") of an instant in a timezone. Used to pre-fill form inputs.
export function zonedParts(at: Date, timezone: string): { date: string; time: string } {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: resolveZone(timezone),
    hourCycle: "h23",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  }).formatToParts(at);
  const field = (type: Intl.DateTimeFormatPartTypes) => parts.find((part) => part.type === type)?.value ?? "00";
  return {
    date: `${field("year")}-${field("month")}-${field("day")}`,
    time: `${field("hour")}:${field("minute")}`,
  };
}

// Converts a wall-clock date and time in a timezone to a UTC ISO string.
// Returns null for malformed input and for a local time that does not exist (the hour skipped by a DST change).
export function zonedToUtcIso(date: string, time: string, timezone: string): string | null {
  const dateMatch = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date);
  const timeMatch = /^(\d{2}):(\d{2})$/.exec(time);
  if (dateMatch === null || timeMatch === null) return null;
  const wallAsUtc = Date.UTC(
    Number(dateMatch[1]),
    Number(dateMatch[2]) - 1,
    Number(dateMatch[3]),
    Number(timeMatch[1]),
    Number(timeMatch[2]),
  );
  // Two passes settle the offset when the zone's offset changes near the target time.
  let instant = wallAsUtc - offsetMinutes(timezone, new Date(wallAsUtc)) * 60_000;
  instant = wallAsUtc - offsetMinutes(timezone, new Date(instant)) * 60_000;
  const check = zonedParts(new Date(instant), timezone);
  if (check.date !== date || check.time !== time) return null;
  return new Date(instant).toISOString();
}

export function timeZoneOptions(): string[] {
  const zones = Intl.supportedValuesOf("timeZone");
  return zones.includes("UTC") ? zones : [...zones, "UTC"];
}
