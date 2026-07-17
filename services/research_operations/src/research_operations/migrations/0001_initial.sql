CREATE TABLE IF NOT EXISTS research_ops.outbox_delivery (
    event_id uuid PRIMARY KEY
        REFERENCES public.portal_webauditevent(id) ON DELETE RESTRICT,
    event_type varchar(128) NOT NULL,
    payload_hash varchar(71) NOT NULL,
    idempotency_key varchar(255) NOT NULL UNIQUE,
    created_at timestamptz NOT NULL,
    status varchar(24) NOT NULL,
    available_at timestamptz NOT NULL,
    attempt_count integer NOT NULL DEFAULT 0,
    last_attempted_at timestamptz NULL,
    last_error_category varchar(64) NOT NULL DEFAULT '',
    last_error varchar(512) NOT NULL DEFAULT '',
    claimed_by varchar(255) NOT NULL DEFAULT '',
    lease_token uuid NULL,
    fencing_token bigint NOT NULL DEFAULT 0,
    lease_expires_at timestamptz NULL,
    projected_at timestamptz NULL,
    dead_letter_at timestamptz NULL,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT research_ops_outbox_payload_hash_valid
        CHECK (payload_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT research_ops_outbox_attempt_count_valid
        CHECK (attempt_count >= 0),
    CONSTRAINT research_ops_outbox_fencing_token_valid
        CHECK (fencing_token >= 0),
    CONSTRAINT research_ops_outbox_status_valid
        CHECK (status IN ('PENDING', 'CLAIMED', 'PROJECTED', 'DEAD_LETTER')),
    CONSTRAINT research_ops_outbox_state_valid CHECK (
        (status = 'PENDING'
            AND claimed_by = '' AND lease_token IS NULL
            AND lease_expires_at IS NULL
            AND projected_at IS NULL AND dead_letter_at IS NULL)
        OR
        (status = 'CLAIMED'
            AND claimed_by <> '' AND lease_token IS NOT NULL
            AND lease_expires_at IS NOT NULL AND fencing_token > 0
            AND projected_at IS NULL AND dead_letter_at IS NULL)
        OR
        (status = 'PROJECTED'
            AND claimed_by = '' AND lease_token IS NULL
            AND lease_expires_at IS NULL
            AND projected_at IS NOT NULL AND dead_letter_at IS NULL)
        OR
        (status = 'DEAD_LETTER'
            AND claimed_by = '' AND lease_token IS NULL
            AND lease_expires_at IS NULL
            AND projected_at IS NULL AND dead_letter_at IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS research_ops_outbox_claimable_idx
    ON research_ops.outbox_delivery(status, available_at, created_at);
CREATE INDEX IF NOT EXISTS research_ops_outbox_lease_idx
    ON research_ops.outbox_delivery(lease_expires_at)
    WHERE status = 'CLAIMED';

CREATE TABLE IF NOT EXISTS research_ops.outbox_operator_action (
    action_id uuid PRIMARY KEY,
    event_id uuid NOT NULL
        REFERENCES research_ops.outbox_delivery(event_id) ON DELETE RESTRICT,
    action varchar(32) NOT NULL CHECK (action IN ('REQUEUE')),
    expected_payload_hash varchar(71) NOT NULL,
    operator_id varchar(255) NOT NULL,
    reason varchar(255) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS research_ops.worker_heartbeat (
    worker_id varchar(255) PRIMARY KEY,
    process_id integer NOT NULL,
    state varchar(24) NOT NULL,
    current_event_id uuid NULL,
    started_at timestamptz NOT NULL,
    last_seen_at timestamptz NOT NULL,
    stopped_at timestamptz NULL,
    CONSTRAINT research_ops_worker_state_valid
        CHECK (state IN ('STARTING', 'IDLE', 'WORKING', 'DRAINING', 'STOPPED'))
);

CREATE INDEX IF NOT EXISTS research_ops_worker_last_seen_idx
    ON research_ops.worker_heartbeat(last_seen_at);

CREATE TABLE IF NOT EXISTS research_ops.experiment_identity (
    authority varchar(128) NOT NULL,
    experiment_id varchar(255) NOT NULL,
    manifest_hash varchar(71) NOT NULL,
    fencing_counter bigint NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (authority, experiment_id),
    CONSTRAINT research_ops_experiment_manifest_hash_valid
        CHECK (manifest_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT research_ops_experiment_fencing_counter_valid
        CHECK (fencing_counter >= 0)
);

CREATE TABLE IF NOT EXISTS research_ops.experiment_request (
    authority varchar(128) NOT NULL,
    experiment_id varchar(255) NOT NULL,
    request_id varchar(255) NOT NULL,
    request_hash varchar(71) NOT NULL,
    owner_id varchar(255) NOT NULL,
    run_id uuid NOT NULL,
    status varchar(24) NOT NULL,
    result_ref varchar(1024) NOT NULL DEFAULT '',
    result_hash varchar(71) NOT NULL DEFAULT '',
    error_code varchar(128) NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    started_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at timestamptz NULL,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (authority, experiment_id, request_id),
    FOREIGN KEY (authority, experiment_id)
        REFERENCES research_ops.experiment_identity(authority, experiment_id)
        ON DELETE RESTRICT,
    CONSTRAINT research_ops_experiment_request_hash_valid
        CHECK (request_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT research_ops_experiment_request_status_valid
        CHECK (status IN ('ACTIVE', 'SUCCEEDED', 'FAILED', 'EXPIRED', 'RELEASED')),
    CONSTRAINT research_ops_experiment_request_result_valid CHECK (
        (status = 'SUCCEEDED' AND result_ref <> ''
            AND result_hash ~ '^sha256:[0-9a-f]{64}$'
            AND finished_at IS NOT NULL)
        OR
        (status IN ('FAILED', 'EXPIRED', 'RELEASED') AND finished_at IS NOT NULL)
        OR
        (status = 'ACTIVE' AND finished_at IS NULL
            AND result_ref = '' AND result_hash = '')
    )
);

CREATE INDEX IF NOT EXISTS research_ops_experiment_request_run_idx
    ON research_ops.experiment_request(run_id);

CREATE TABLE IF NOT EXISTS research_ops.active_experiment_claim (
    authority varchar(128) NOT NULL,
    experiment_id varchar(255) NOT NULL,
    request_id varchar(255) NOT NULL,
    request_hash varchar(71) NOT NULL,
    owner_id varchar(255) NOT NULL,
    run_id uuid NOT NULL,
    lease_token uuid NOT NULL,
    fencing_token bigint NOT NULL,
    lease_expires_at timestamptz NOT NULL,
    heartbeat_at timestamptz NOT NULL,
    started_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (authority, experiment_id),
    FOREIGN KEY (authority, experiment_id, request_id)
        REFERENCES research_ops.experiment_request(authority, experiment_id, request_id)
        ON DELETE RESTRICT,
    CONSTRAINT research_ops_active_claim_fencing_token_valid
        CHECK (fencing_token > 0)
);

CREATE INDEX IF NOT EXISTS research_ops_active_claim_lease_idx
    ON research_ops.active_experiment_claim(lease_expires_at);
