-- Case revision (F2, D-16; plans/f0/api-draft.md 3.5). A new case starts at revision 1. Each change to the title raises it by one.
-- A change sends the revision it read in If-Match; the server refuses a stale value with 412. Other changes to the case
-- (close, reopen, assignments, deadlines, classification) do not raise the revision yet.

ALTER TABLE cases ADD COLUMN revision integer NOT NULL DEFAULT 1 CHECK (revision >= 1);
