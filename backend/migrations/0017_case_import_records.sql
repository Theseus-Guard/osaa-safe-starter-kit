-- Case import registry: one row per imported case record, keyed by the stable case_id the record carries.
-- The registry tells a repeated import (duplicate) from a different record that reuses the same ID (conflict).
-- The record is kept as received in payload, so no field is dropped silently. Only the mapped fields are on the case row.
-- Every table keeps the standard columns; the application never deletes rows.

CREATE TABLE case_import_records (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    source_case_id uuid NOT NULL,
    case_id uuid NOT NULL REFERENCES cases (id),
    record_schema_version text NOT NULL CHECK (char_length(record_schema_version) BETWEEN 1 AND 20),
    content_sha256 text NOT NULL CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    imported_by uuid NOT NULL REFERENCES users (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT case_import_records_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

-- One active registry row per source case. A different record with the same ID is a conflict, never a second case.
CREATE UNIQUE INDEX case_import_records_source_active_unique ON case_import_records (source_case_id) WHERE NOT is_deleted;
CREATE INDEX case_import_records_case_idx ON case_import_records (case_id) WHERE NOT is_deleted;

CREATE TRIGGER case_import_records_set_updated_at BEFORE UPDATE ON case_import_records FOR EACH ROW EXECUTE FUNCTION set_updated_at();
