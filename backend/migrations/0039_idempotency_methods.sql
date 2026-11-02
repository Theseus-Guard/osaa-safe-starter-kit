-- Idempotency-Key records for state-changing PUT and PATCH requests too (plans/f0/api-draft.md section 3.4).
-- Migration 0032 allowed the method POST only. The case routes that replace or change a resource in place (PUT
-- classification, PUT impact values, PATCH deadlines, PATCH legal duties, PATCH review layers) need the same key rules,
-- so the allowed methods are widened. The scope and the stored response are unchanged.
-- Forward only: migration 0032 is not edited.

ALTER TABLE idempotency_keys DROP CONSTRAINT idempotency_keys_method_check;
ALTER TABLE idempotency_keys ADD CONSTRAINT idempotency_keys_method_check CHECK (method IN ('POST', 'PUT', 'PATCH'));
