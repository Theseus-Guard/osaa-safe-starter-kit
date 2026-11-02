-- Revisions of three rows changed with If-Match (F2, D-16; plans/f0/api-draft.md 3.5): a deadline task, a legal duty and
-- a user. A new row starts at revision 1. Each change to the row raises it by one in the same transaction; a change that
-- changes nothing keeps it. The case revision (0048) is not raised by changes to its deadline tasks or legal duties.

ALTER TABLE deadline_tasks ADD COLUMN revision integer NOT NULL DEFAULT 1 CHECK (revision >= 1);
ALTER TABLE legal_duties ADD COLUMN revision integer NOT NULL DEFAULT 1 CHECK (revision >= 1);
ALTER TABLE users ADD COLUMN revision integer NOT NULL DEFAULT 1 CHECK (revision >= 1);
