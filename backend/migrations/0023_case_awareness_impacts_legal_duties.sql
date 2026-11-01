-- Case record, part 1: awareness events, impact assessments, and legal duties (plans/01 and plans/02).
-- awareness_events are append-only facts: the operator, an affected party, or an external report made the organization
-- aware. An incident is told apart from a near miss and a blocked attempt by event_kind.
-- impacts hold one assessment per impact area. An area without an active row is "not assessed", never "no impact";
-- "unknown" is an explicit severity value.
-- legal_duties are statutory or contractual duties with their own due times. They never change deadline_tasks.
-- Every table keeps the standard columns and the updated_at trigger; the application never deletes rows.
-- Grants come from the default privileges set up for the migrator role (db/init), so this migration adds none.

CREATE TABLE awareness_events (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES cases (id),
    awareness_kind text NOT NULL CHECK (awareness_kind IN ('operator', 'affected_party', 'external_report')),
    event_kind text NOT NULL CHECK (event_kind IN ('incident', 'near_miss', 'blocked_attempt')),
    occurred_at timestamptz NOT NULL,
    basis text NOT NULL CHECK (char_length(basis) BETWEEN 1 AND 1000),
    recorded_by uuid NOT NULL REFERENCES users (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT awareness_events_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE INDEX awareness_events_case_idx ON awareness_events (case_id, occurred_at, id) WHERE NOT is_deleted;

CREATE TRIGGER awareness_events_set_updated_at BEFORE UPDATE ON awareness_events FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE impacts (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES cases (id),
    area text NOT NULL CHECK (area IN (
        'affected_people_groups',
        'severity',
        'scale',
        'duration',
        'reversibility',
        'disproportionate_effect'
    )),
    severity text NOT NULL CHECK (severity IN ('none', 'low', 'medium', 'high', 'critical', 'unknown')),
    scope text NOT NULL CHECK (char_length(scope) BETWEEN 1 AND 1000),
    updated_by uuid NOT NULL REFERENCES users (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT impacts_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

-- One active assessment per area. A removed assessment is a soft delete, so the area reads as not assessed again.
CREATE UNIQUE INDEX impacts_active_area_unique ON impacts (case_id, area) WHERE NOT is_deleted;

CREATE TRIGGER impacts_set_updated_at BEFORE UPDATE ON impacts FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE legal_duties (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES cases (id),
    kind text NOT NULL CHECK (kind IN ('statutory', 'contractual')),
    description text NOT NULL CHECK (char_length(description) BETWEEN 1 AND 1000),
    source_reference text NOT NULL CHECK (char_length(source_reference) BETWEEN 1 AND 500),
    jurisdiction text CHECK (jurisdiction IS NULL OR char_length(jurisdiction) BETWEEN 1 AND 200),
    due_at timestamptz NOT NULL,
    status text NOT NULL CHECK (status IN ('open', 'done')),
    created_by uuid NOT NULL REFERENCES users (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT legal_duties_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE INDEX legal_duties_case_idx ON legal_duties (case_id, due_at, id) WHERE NOT is_deleted;

CREATE TRIGGER legal_duties_set_updated_at BEFORE UPDATE ON legal_duties FOR EACH ROW EXECUTE FUNCTION set_updated_at();
