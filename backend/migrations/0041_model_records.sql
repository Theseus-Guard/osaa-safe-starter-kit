-- Model, runtime and reasoning record (F2, package T05; plans/01 R05, plans/02 evidence categories).
--
-- A record states which model, runtime and provider alias produced an agent run, and how reasoning was configured.
-- It is the observed configuration, so a later replay can be run with the same settings. It is not the reasoning
-- text: raw reasoning or chain-of-thought is never collected here, and no prompt, secret or personal data is stored.
--
-- Records are versioned and insert-only, like case_classifications (migration 0024). A revocation inserts the next
-- version of the same record with revoked = true and the reason; the earlier versions are never changed. A revoked
-- record stays readable for audit and is never used as the current configuration.
-- A record concerns the case as a whole, or one evidence item, or one review round (never both).
-- Every table keeps the standard columns; the application never deletes rows. Grants come from the default privileges
-- set up for the migrator role (db/init), so this migration adds none.

CREATE TABLE model_records (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    -- The stable ID of the record. Every version of one record carries the same record_id.
    record_id uuid NOT NULL,
    case_id uuid NOT NULL REFERENCES cases (id),
    evidence_item_id uuid REFERENCES evidence_items (id),
    review_round_id uuid REFERENCES review_rounds (id),
    version integer NOT NULL CHECK (version >= 1),
    -- Identifiers only: letters, digits and . _ : / @ + - (no spaces, so no prompt text fits here).
    model_name text NOT NULL CHECK (model_name ~ '^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$'),
    model_version text NOT NULL CHECK (model_version ~ '^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$'),
    runtime text NOT NULL CHECK (runtime ~ '^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$'),
    provider_alias text NOT NULL CHECK (provider_alias ~ '^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,199}$'),
    -- The reasoning configuration: whether reasoning was on, and the effort or budget when it was on.
    reasoning_mode text NOT NULL CHECK (reasoning_mode IN ('disabled', 'enabled', 'unknown')),
    reasoning_effort text CHECK (reasoning_effort IS NULL OR reasoning_effort IN ('low', 'medium', 'high', 'max')),
    reasoning_budget_tokens integer CHECK (reasoning_budget_tokens IS NULL OR reasoning_budget_tokens BETWEEN 1 AND 10000000),
    -- Set by the server when the record is written. It is the time of the record, not of the run.
    recorded_at timestamptz NOT NULL DEFAULT now(),
    revoked boolean NOT NULL DEFAULT false,
    revoked_at timestamptz,
    revocation_reason text CHECK (revocation_reason IS NULL OR char_length(revocation_reason) BETWEEN 1 AND 1000),
    created_by uuid NOT NULL REFERENCES users (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT model_records_one_link CHECK (evidence_item_id IS NULL OR review_round_id IS NULL),
    CONSTRAINT model_records_version_unique UNIQUE (record_id, version),
    CONSTRAINT model_records_effort_needs_reasoning CHECK (
        (reasoning_effort IS NULL AND reasoning_budget_tokens IS NULL) OR reasoning_mode = 'enabled'
    ),
    CONSTRAINT model_records_revocation_consistent CHECK (
        (NOT revoked AND revoked_at IS NULL AND revocation_reason IS NULL)
        OR (revoked AND revoked_at IS NOT NULL AND revocation_reason IS NOT NULL)
    ),
    CONSTRAINT model_records_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE INDEX model_records_case_idx ON model_records (case_id, record_id, version DESC) WHERE NOT is_deleted;

CREATE TRIGGER model_records_set_updated_at BEFORE UPDATE ON model_records FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE FUNCTION reject_model_record_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (NEW.id, NEW.record_id, NEW.case_id, NEW.evidence_item_id, NEW.review_round_id, NEW.version,
        NEW.model_name, NEW.model_version, NEW.runtime, NEW.provider_alias,
        NEW.reasoning_mode, NEW.reasoning_effort, NEW.reasoning_budget_tokens, NEW.recorded_at,
        NEW.revoked, NEW.revoked_at, NEW.revocation_reason, NEW.created_by, NEW.created_at)
        IS DISTINCT FROM
       (OLD.id, OLD.record_id, OLD.case_id, OLD.evidence_item_id, OLD.review_round_id, OLD.version,
        OLD.model_name, OLD.model_version, OLD.runtime, OLD.provider_alias,
        OLD.reasoning_mode, OLD.reasoning_effort, OLD.reasoning_budget_tokens, OLD.recorded_at,
        OLD.revoked, OLD.revoked_at, OLD.revocation_reason, OLD.created_by, OLD.created_at) THEN
        RAISE EXCEPTION 'model records are append-only: their content cannot be changed';
    END IF;
    IF NOT (NOT OLD.is_deleted AND NEW.is_deleted) AND (
        NEW.is_deleted IS DISTINCT FROM OLD.is_deleted OR NEW.deleted_at IS DISTINCT FROM OLD.deleted_at
    ) THEN
        RAISE EXCEPTION 'model records are append-only: a soft delete cannot be undone or re-dated';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER model_records_append_only BEFORE UPDATE ON model_records
    FOR EACH ROW EXECUTE FUNCTION reject_model_record_change();
