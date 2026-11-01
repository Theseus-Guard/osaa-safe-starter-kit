-- Classification, part 3: "none applies" is an explicit trigger value (plans/01 R01).
-- A classification whose only trigger is none_applies records that no trigger applies. An empty trigger list still
-- means that the triggers were not assessed. none_applies cannot be combined with another trigger; the API refuses that
-- with 422, and the constraint below enforces it in the database too.
-- Versions are never changed, so this migration only replaces the check constraint on triggers. Forward only.
-- Grants are unchanged.

ALTER TABLE case_classifications DROP CONSTRAINT case_classifications_triggers_check;

ALTER TABLE case_classifications ADD CONSTRAINT case_classifications_triggers_check CHECK (triggers <@ ARRAY[
    'unauthorized_access_or_change',
    'boundary_escape',
    'confidential_data_exposure',
    'out_of_scope_continuation',
    'harm_inaction_near_miss',
    'none_applies'
]::text[]);

ALTER TABLE case_classifications ADD CONSTRAINT case_classifications_none_applies_alone CHECK (
    NOT ('none_applies' = ANY (triggers)) OR cardinality(triggers) = 1
);
