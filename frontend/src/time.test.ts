import { describe, expect, it } from "vitest";
import {
  DEFAULT_ZONE,
  formatClock,
  formatInTimezone,
  formatUtcPlain,
  offsetLabel,
  timeZoneOptions,
  zonedParts,
  zonedToUtcIso,
} from "./time";

// Fixed instant used across the tests: 2026-11-01 06:05:42 UTC.
const INSTANT = new Date("2026-11-01T06:05:42.000Z");
const ISTANBUL = "Europe/Istanbul";
const NEW_YORK = "America/New_York";

describe("offsetLabel", () => {
  it("shows a positive whole-hour offset", () => {
    expect(offsetLabel(ISTANBUL, INSTANT)).toBe("UTC+03:00");
  });

  it("shows a negative offset with a sign", () => {
    expect(offsetLabel(NEW_YORK, new Date("2026-07-01T12:00:00Z"))).toBe("UTC-04:00");
  });

  it("shows a fractional offset in minutes", () => {
    expect(offsetLabel("Asia/Kathmandu", INSTANT)).toBe("UTC+05:45");
    expect(offsetLabel("America/St_Johns", new Date("2026-01-15T12:00:00Z"))).toBe("UTC-03:30");
  });

  it("shows zero for UTC", () => {
    expect(offsetLabel("UTC", INSTANT)).toBe("UTC+00:00");
  });

  it("uses the offset in force at the given instant", () => {
    expect(offsetLabel(NEW_YORK, new Date("2026-01-15T12:00:00Z"))).toBe("UTC-05:00");
  });
});

describe("formatInTimezone", () => {
  it("formats the wall clock with the offset label", () => {
    expect(formatInTimezone("2026-11-01T06:05:42Z", ISTANBUL)).toBe("Nov 1, 2026, 09:05 UTC+03:00");
  });

  it("falls back to UTC for a zone name the runtime does not know", () => {
    expect(formatInTimezone("2026-11-01T06:05:42Z", "Mars/Olympus_Mons")).toBe("Nov 1, 2026, 06:05 UTC+00:00");
  });

  it("returns the input unchanged when the timestamp cannot be parsed", () => {
    expect(formatInTimezone("not-a-date", ISTANBUL)).toBe("not-a-date");
  });
});

describe("formatClock", () => {
  it("shows seconds and the offset", () => {
    expect(formatClock(INSTANT, ISTANBUL)).toBe("09:05:42 (UTC+03:00)");
  });

  it("falls back to UTC for an unknown zone", () => {
    expect(formatClock(INSTANT, "Mars/Olympus_Mons")).toBe("06:05:42 (UTC+00:00)");
  });
});

describe("formatUtcPlain", () => {
  it("drops the seconds and states UTC", () => {
    expect(formatUtcPlain("2026-11-01T06:05:42Z")).toBe("2026-11-01 06:05 UTC");
  });

  it("returns the input unchanged when the timestamp cannot be parsed", () => {
    expect(formatUtcPlain("garbage")).toBe("garbage");
  });
});

describe("zonedParts", () => {
  it("returns the wall-clock date and time in the zone", () => {
    expect(zonedParts(INSTANT, ISTANBUL)).toEqual({ date: "2026-11-01", time: "09:05" });
  });

  it("can roll the date over relative to UTC", () => {
    expect(zonedParts(new Date("2026-11-01T22:30:00Z"), ISTANBUL)).toEqual({ date: "2026-11-02", time: "01:30" });
  });

  it("uses UTC for an unknown zone", () => {
    expect(zonedParts(INSTANT, "Mars/Olympus_Mons")).toEqual({ date: "2026-11-01", time: "06:05" });
  });
});

describe("zonedToUtcIso", () => {
  it("converts a wall-clock time in a zone to UTC", () => {
    expect(zonedToUtcIso("2026-11-01", "09:05", ISTANBUL)).toBe("2026-11-01T06:05:00.000Z");
  });

  it("round-trips with zonedParts", () => {
    const instant = new Date("2026-07-01T12:00:00Z");
    const parts = zonedParts(instant, NEW_YORK);
    expect(zonedToUtcIso(parts.date, parts.time, NEW_YORK)).toBe("2026-07-01T12:00:00.000Z");
  });

  it("resolves an ambiguous fall-back time to the earlier instant", () => {
    // 2026-11-01 01:30 happens twice in New York. The first occurrence is still on EDT (UTC-4).
    expect(zonedToUtcIso("2026-11-01", "01:30", NEW_YORK)).toBe("2026-11-01T05:30:00.000Z");
  });

  it("uses the offset in force on the target date across a DST change", () => {
    // New York is on EDT (UTC-4) in late October and EST (UTC-5) after the change.
    expect(zonedToUtcIso("2026-10-30", "12:00", NEW_YORK)).toBe("2026-10-30T16:00:00.000Z");
    expect(zonedToUtcIso("2026-11-10", "12:00", NEW_YORK)).toBe("2026-11-10T17:00:00.000Z");
  });

  it("returns null for a local time that does not exist (DST gap)", () => {
    // 2026-03-08 02:30 does not exist in New York: the clock jumps from 02:00 to 03:00.
    expect(zonedToUtcIso("2026-03-08", "02:30", NEW_YORK)).toBeNull();
  });

  it("returns null for a date that does not exist", () => {
    expect(zonedToUtcIso("2026-02-30", "09:05", ISTANBUL)).toBeNull();
  });

  it("returns null for malformed date or time input", () => {
    expect(zonedToUtcIso("2026-1-1", "09:05", ISTANBUL)).toBeNull();
    expect(zonedToUtcIso("2026-11-01", "9:05", ISTANBUL)).toBeNull();
    expect(zonedToUtcIso("", "", ISTANBUL)).toBeNull();
  });
});

describe("timeZoneOptions", () => {
  it("always includes UTC", () => {
    expect(timeZoneOptions()).toContain("UTC");
  });

  it("lists the runtime zones and adds UTC only when it is missing", () => {
    const options = timeZoneOptions();
    expect(options).toContain(ISTANBUL);
    expect(options.filter((zone) => zone === "UTC")).toHaveLength(1);
    expect(DEFAULT_ZONE).toBe("UTC");
  });
});
