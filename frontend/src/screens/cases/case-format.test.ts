import { describe, expect, it } from "vitest";
import {
  custodyActionLabel,
  describeDetails,
  evidenceStatusLabel,
  formatBytes,
  historyActionLabel,
  humanize,
  missingFieldLabel,
  recordText,
  triggerLabel,
} from "./case-format";

describe("case record labels", () => {
  it("labels each R01 trigger in plain words", () => {
    expect(triggerLabel("harm_inaction_near_miss")).toBe("Harm, inaction or near miss");
    expect(triggerLabel("boundary_escape")).toBe("Boundary escape");
  });

  it("shows a null record text as not recorded for the full view and as hidden otherwise", () => {
    expect(recordText(null, true)).toBe("Not recorded.");
    expect(recordText(null, false)).toBe("Not shown for your role.");
    expect(recordText("Contained", false)).toBe("Contained");
  });

  it("turns each missing field of the preliminary report into a label", () => {
    expect(missingFieldLabel({ field: "contact", state: "missing" })).toBe("Reporter contact");
    expect(missingFieldLabel({ field: "awareness_events", state: "missing" })).toBe("Awareness events");
    expect(missingFieldLabel({ field: "impacts.disproportionate_effect", state: "missing" })).toBe(
      "Impact: Disproportionate effect",
    );
    expect(missingFieldLabel({ field: "classification.evidence_gap_plan", state: "unknown" })).toBe(
      "Classification: Evidence gap plan (unknown)",
    );
  });
});

describe("humanize", () => {
  it("turns a snake_case value into a sentence", () => {
    expect(humanize("preliminary_safe_report")).toBe("Preliminary safe report");
    expect(humanize("not_applicable")).toBe("Not applicable");
  });
});

describe("labels", () => {
  it("labels known history actions and falls back for unknown ones", () => {
    expect(historyActionLabel("case.closed")).toBe("Case closed");
    expect(historyActionLabel("case.new_thing")).toBe("Case new thing");
  });

  it("labels custody actions and evidence statuses", () => {
    expect(custodyActionLabel("verified")).toBe("Integrity verified");
    expect(custodyActionLabel("exported")).toBe("Exported");
    expect(evidenceStatusLabel("available")).toBe("Available");
    expect(evidenceStatusLabel("archived")).toBe("Archived");
  });

  it("does not treat inherited object keys as labels", () => {
    expect(historyActionLabel("constructor")).toBe("Constructor");
    expect(custodyActionLabel("toString")).toBe("ToString");
  });
});

describe("formatBytes", () => {
  it("uses bytes, kilobytes and megabytes", () => {
    expect(formatBytes(512)).toBe("512 B");
    expect(formatBytes(2048)).toBe("2.0 KB");
    expect(formatBytes(5 * 1024 * 1024)).toBe("5.0 MB");
  });
});

describe("describeDetails", () => {
  it("returns nothing when the caller cannot see details", () => {
    expect(describeDetails(null)).toEqual([]);
  });

  it("reads a change as old and new values", () => {
    expect(describeDetails({ status: { old: "open", new: "closed" } })).toEqual(["Status: open -> closed"]);
  });

  it("shows free text as a character count and null as none", () => {
    expect(describeDetails({ close_reason: { old: null, new: { length: 12 } } })).toEqual([
      "Close reason: none -> 12 characters",
    ]);
  });

  it("shows plain values and nested objects", () => {
    expect(describeDetails({ task_id: "task-1", kind: "weekly_update", extra: { a: 1 } })).toEqual([
      "Task id: task-1",
      "Kind: weekly_update",
      'Extra: {"a":1}',
    ]);
  });
});
