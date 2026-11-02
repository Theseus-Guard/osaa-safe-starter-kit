-- Review rounds and layers. A round is one review of a case. Opening a round creates one entry per layer of the
-- review template (eleven layers, all not set). A layer outcome is set with evidence references and a reason. The
-- reason is required for every outcome except pass. Every change to a layer increments the round revision in the same
-- transaction; a write with a stale expected_revision is refused.
-- No overall result is stored: a round has no single compliance value.
--
-- The F1 case_reviews table is kept as it is. Each of its rows, including soft-deleted ones, is copied into one closed
-- round with a single legacy layer entry, so no review history is lost. The copy is idempotent and deletes nothing.
-- Every table keeps the standard columns; the application never deletes rows.

CREATE TABLE review_rounds (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id uuid NOT NULL REFERENCES cases (id),
    status text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    -- Starts at 1 and increases by one for every change to a layer of this round.
    revision integer NOT NULL DEFAULT 1 CHECK (revision >= 1),
    opened_by uuid NOT NULL REFERENCES users (id),
    opened_at timestamptz NOT NULL DEFAULT now(),
    closed_at timestamptz,
    -- Set only on the round copied from a case_reviews row.
    legacy_case_review_id uuid REFERENCES case_reviews (id),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    CONSTRAINT review_rounds_closed_consistent CHECK ((status = 'closed') = (closed_at IS NOT NULL)),
    CONSTRAINT review_rounds_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE INDEX review_rounds_case_idx ON review_rounds (case_id, opened_at DESC, id) WHERE NOT is_deleted;

CREATE UNIQUE INDEX review_rounds_legacy_unique ON review_rounds (legacy_case_review_id)
    WHERE legacy_case_review_id IS NOT NULL;

CREATE TRIGGER review_rounds_set_updated_at BEFORE UPDATE ON review_rounds FOR EACH ROW EXECUTE FUNCTION set_updated_at();

CREATE TABLE review_layers (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    round_id uuid NOT NULL REFERENCES review_rounds (id),
    -- The eleven template layers, as their URL keys. legacy-case-review is the one entry of a migrated round.
    layer text NOT NULL CHECK (layer IN (
        'model', 'instructions', 'safeguards', 'tools', 'environment', 'monitoring', 'human-operations',
        'supply-chain', 'serving-path', 'authorization-and-commitment', 'workflow-composition',
        'legacy-case-review'
    )),
    -- Null until the layer is set.
    outcome text CHECK (outcome IS NULL OR outcome IN ('pass', 'fail', 'insufficient_evidence', 'not_applicable')),
    reason text CHECK (reason IS NULL OR char_length(reason) BETWEEN 1 AND 1000),
    -- Identifiers of evidence items of the same case. The application checks them; the array has no foreign key.
    evidence_ids uuid[] NOT NULL DEFAULT '{}',
    set_by uuid REFERENCES users (id),
    set_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    deleted_at timestamptz,
    is_deleted boolean NOT NULL DEFAULT false,
    -- A layer is either not set (no outcome, reason, setter, or time) or set (outcome, setter, and time).
    CONSTRAINT review_layers_set_consistent CHECK ((outcome IS NULL) = (set_by IS NULL) AND (outcome IS NULL) = (set_at IS NULL)),
    CONSTRAINT review_layers_reason_only_when_set CHECK (outcome IS NOT NULL OR reason IS NULL),
    -- The reason is required for every outcome except pass. The migrated legacy entry is exempt: its history may lack one.
    CONSTRAINT review_layers_reason_required CHECK (
        layer = 'legacy-case-review' OR outcome IS NULL OR outcome = 'pass' OR reason IS NOT NULL
    ),
    CONSTRAINT review_layers_deleted_consistent CHECK ((is_deleted AND deleted_at IS NOT NULL) OR (NOT is_deleted AND deleted_at IS NULL))
);

CREATE UNIQUE INDEX review_layers_round_layer_unique ON review_layers (round_id, layer) WHERE NOT is_deleted;

CREATE TRIGGER review_layers_set_updated_at BEFORE UPDATE ON review_layers FOR EACH ROW EXECUTE FUNCTION set_updated_at();

-- BEGIN legacy backfill (idempotent: each case_reviews row is copied once)
INSERT INTO review_rounds (
    case_id, status, revision, opened_by, opened_at, closed_at, legacy_case_review_id,
    created_at, updated_at, deleted_at, is_deleted
)
SELECT cr.case_id, 'closed', 1, cr.reviewer_id, cr.created_at, cr.created_at, cr.id,
       cr.created_at, cr.updated_at, cr.deleted_at, cr.is_deleted
FROM case_reviews cr
WHERE NOT EXISTS (SELECT 1 FROM review_rounds rr WHERE rr.legacy_case_review_id = cr.id);

INSERT INTO review_layers (
    round_id, layer, outcome, reason, evidence_ids, set_by, set_at, created_at, updated_at, deleted_at, is_deleted
)
SELECT rr.id, 'legacy-case-review', cr.outcome, cr.note, '{}', cr.reviewer_id, cr.created_at,
       cr.created_at, cr.updated_at, cr.deleted_at, cr.is_deleted
FROM case_reviews cr
JOIN review_rounds rr ON rr.legacy_case_review_id = cr.id
WHERE NOT EXISTS (SELECT 1 FROM review_layers rl WHERE rl.round_id = rr.id);
-- END legacy backfill
