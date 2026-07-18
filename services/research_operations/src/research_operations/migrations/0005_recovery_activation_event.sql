CREATE TABLE research_ops.recovery_activation_event (
    activation_id uuid PRIMARY KEY,
    backup_id uuid NOT NULL,
    backup_manifest_hash varchar(71) NOT NULL UNIQUE,
    recovery_receipt_hash varchar(71) NOT NULL UNIQUE,
    release_bundle_digest varchar(71) NOT NULL,
    requested_by varchar(255) NOT NULL,
    reason varchar(255) NOT NULL,
    activated_at timestamptz NOT NULL,
    prior_state varchar(16) NOT NULL,
    new_state varchar(16) NOT NULL,
    prior_generation bigint NOT NULL,
    new_generation bigint NOT NULL UNIQUE,
    prior_fence_token_hash varchar(71) NOT NULL,
    content_hash varchar(71) NOT NULL UNIQUE,
    CONSTRAINT research_ops_recovery_activation_manifest_hash_valid
        CHECK (backup_manifest_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT research_ops_recovery_activation_receipt_hash_valid
        CHECK (recovery_receipt_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT research_ops_recovery_activation_bundle_digest_valid
        CHECK (release_bundle_digest ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT research_ops_recovery_activation_operator_valid
        CHECK (
            requested_by <> ''
            AND requested_by = btrim(requested_by)
        ),
    CONSTRAINT research_ops_recovery_activation_reason_valid
        CHECK (reason = 'signed_recovery_activation'),
    CONSTRAINT research_ops_recovery_activation_transition_valid
        CHECK (
            prior_state = 'SEALED'
            AND new_state = 'OPEN'
            AND prior_generation > 0
            AND new_generation = prior_generation + 1
        ),
    CONSTRAINT research_ops_recovery_activation_fence_hash_valid
        CHECK (prior_fence_token_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT research_ops_recovery_activation_content_hash_valid
        CHECK (content_hash ~ '^sha256:[0-9a-f]{64}$')
);

CREATE INDEX research_ops_recovery_activation_time_idx
    ON research_ops.recovery_activation_event(activated_at DESC);

CREATE FUNCTION research_ops.reject_recovery_activation_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'recovery_activation_event_append_only'
        USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER research_ops_recovery_activation_event_append_only
BEFORE UPDATE OR DELETE ON research_ops.recovery_activation_event
FOR EACH ROW
EXECUTE FUNCTION research_ops.reject_recovery_activation_event_mutation();
