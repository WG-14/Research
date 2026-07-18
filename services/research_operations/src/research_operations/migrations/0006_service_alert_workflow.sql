CREATE TABLE research_ops.service_alert (
    alert_id uuid PRIMARY KEY,
    idempotency_key varchar(255) NOT NULL UNIQUE,
    binding_hash varchar(71) NOT NULL,
    condition_code varchar(128) NOT NULL,
    severity varchar(16) NOT NULL,
    source_actor_id varchar(255) NOT NULL,
    status varchar(24) NOT NULL,
    opened_at timestamptz NOT NULL,
    acknowledgment_deadline_at timestamptz NOT NULL,
    acknowledged_by varchar(255) NOT NULL DEFAULT '',
    acknowledgment_reason varchar(128) NOT NULL DEFAULT '',
    acknowledged_at timestamptz NULL,
    resolved_by varchar(255) NOT NULL DEFAULT '',
    resolution_reason varchar(128) NOT NULL DEFAULT '',
    resolved_at timestamptz NULL,
    escalation_level integer NOT NULL DEFAULT 0,
    last_event_hash varchar(71) NOT NULL,
    updated_at timestamptz NOT NULL,
    CONSTRAINT research_ops_service_alert_binding_hash_valid
        CHECK (binding_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT research_ops_service_alert_idempotency_key_valid
        CHECK (idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$'),
    CONSTRAINT research_ops_service_alert_condition_valid CHECK (
        condition_code IN (
            'audit_validation_failed',
            'backup_failed',
            'backup_stale',
            'certificate_expiry',
            'database_not_primary',
            'database_unavailable',
            'dead_letter_present',
            'job_receipt_unapplied',
            'migration_drift',
            'outbox_lag',
            'outbox_worker_missing',
            'preflight_failed',
            'quarantine',
            'readiness_failed',
            'research_worker_missing',
            'restore_drill_stale',
            'restore_rehearsal_failed',
            'worker_process_failed'
        )
    ),
    CONSTRAINT research_ops_service_alert_severity_valid
        CHECK (severity IN ('WARNING', 'CRITICAL')),
    CONSTRAINT research_ops_service_alert_source_actor_valid
        CHECK (
            source_actor_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$'
        ),
    CONSTRAINT research_ops_service_alert_status_valid
        CHECK (status IN ('OPEN', 'ACKNOWLEDGED', 'RESOLVED')),
    CONSTRAINT research_ops_service_alert_escalation_level_valid
        CHECK (escalation_level >= 0 AND escalation_level <= 32),
    CONSTRAINT research_ops_service_alert_last_event_hash_valid
        CHECK (last_event_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT research_ops_service_alert_time_order_valid CHECK (
        acknowledgment_deadline_at > opened_at
        AND updated_at >= opened_at
    ),
    CONSTRAINT research_ops_service_alert_state_valid CHECK (
        (
            status = 'OPEN'
            AND acknowledged_by = ''
            AND acknowledgment_reason = ''
            AND acknowledged_at IS NULL
            AND resolved_by = ''
            AND resolution_reason = ''
            AND resolved_at IS NULL
        )
        OR
        (
            status = 'ACKNOWLEDGED'
            AND acknowledged_by <> ''
            AND acknowledgment_reason ~ '^[a-z][a-z0-9_]{0,127}$'
            AND acknowledged_at IS NOT NULL
            AND acknowledged_at >= opened_at
            AND resolved_by = ''
            AND resolution_reason = ''
            AND resolved_at IS NULL
        )
        OR
        (
            status = 'RESOLVED'
            AND acknowledged_by <> ''
            AND acknowledgment_reason ~ '^[a-z][a-z0-9_]{0,127}$'
            AND acknowledged_at IS NOT NULL
            AND resolved_by <> ''
            AND resolution_reason ~ '^[a-z][a-z0-9_]{0,127}$'
            AND resolved_at IS NOT NULL
            AND resolved_at >= acknowledged_at
        )
    )
);

CREATE INDEX research_ops_service_alert_due_idx
    ON research_ops.service_alert(
        acknowledgment_deadline_at,
        opened_at,
        alert_id
    )
    WHERE status = 'OPEN';

CREATE TABLE research_ops.service_alert_delivery (
    delivery_id uuid PRIMARY KEY,
    alert_id uuid NOT NULL
        REFERENCES research_ops.service_alert(alert_id) ON DELETE RESTRICT,
    delivery_key varchar(255) NOT NULL UNIQUE,
    endpoint_id varchar(128) NOT NULL,
    escalation_level integer NOT NULL,
    status varchar(24) NOT NULL,
    available_at timestamptz NOT NULL,
    attempt_count integer NOT NULL DEFAULT 0,
    claimed_by varchar(255) NOT NULL DEFAULT '',
    lease_token uuid NULL,
    fencing_token bigint NOT NULL DEFAULT 0,
    lease_expires_at timestamptz NULL,
    response_code integer NULL,
    last_error_code varchar(128) NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL,
    delivered_at timestamptz NULL,
    updated_at timestamptz NOT NULL,
    UNIQUE (alert_id, endpoint_id, escalation_level),
    CONSTRAINT research_ops_service_alert_delivery_key_valid
        CHECK (delivery_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$'),
    CONSTRAINT research_ops_service_alert_endpoint_valid
        CHECK (endpoint_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$'),
    CONSTRAINT research_ops_service_alert_delivery_level_valid
        CHECK (escalation_level >= 0 AND escalation_level <= 32),
    CONSTRAINT research_ops_service_alert_delivery_status_valid
        CHECK (status IN ('PENDING', 'CLAIMED', 'DELIVERED', 'FAILED')),
    CONSTRAINT research_ops_service_alert_delivery_attempt_valid
        CHECK (attempt_count >= 0 AND attempt_count <= 100),
    CONSTRAINT research_ops_service_alert_delivery_fence_valid
        CHECK (fencing_token >= 0),
    CONSTRAINT research_ops_service_alert_delivery_error_valid CHECK (
        last_error_code = ''
        OR last_error_code ~ '^[a-z][a-z0-9_]{0,127}$'
    ),
    CONSTRAINT research_ops_service_alert_delivery_time_valid CHECK (
        available_at >= created_at AND updated_at >= created_at
    ),
    CONSTRAINT research_ops_service_alert_delivery_state_valid CHECK (
        (
            status = 'PENDING'
            AND claimed_by = ''
            AND lease_token IS NULL
            AND lease_expires_at IS NULL
            AND response_code IS NULL
            AND delivered_at IS NULL
        )
        OR
        (
            status = 'CLAIMED'
            AND claimed_by <> ''
            AND lease_token IS NOT NULL
            AND lease_expires_at IS NOT NULL
            AND fencing_token > 0
            AND response_code IS NULL
            AND delivered_at IS NULL
        )
        OR
        (
            status = 'DELIVERED'
            AND claimed_by = ''
            AND lease_token IS NULL
            AND lease_expires_at IS NULL
            AND response_code BETWEEN 200 AND 299
            AND delivered_at IS NOT NULL
        )
        OR
        (
            status = 'FAILED'
            AND claimed_by = ''
            AND lease_token IS NULL
            AND lease_expires_at IS NULL
            AND response_code IS NULL
            AND delivered_at IS NULL
            AND last_error_code <> ''
        )
    )
);

CREATE INDEX research_ops_service_alert_delivery_claim_idx
    ON research_ops.service_alert_delivery(status, available_at, created_at)
    WHERE status IN ('PENDING', 'CLAIMED');

CREATE TABLE research_ops.service_alert_event (
    event_id uuid PRIMARY KEY,
    alert_id uuid NOT NULL
        REFERENCES research_ops.service_alert(alert_id) ON DELETE RESTRICT,
    sequence integer NOT NULL,
    event_type varchar(32) NOT NULL,
    actor_id varchar(255) NOT NULL,
    reason_code varchar(128) NOT NULL,
    occurred_at timestamptz NOT NULL,
    details_hash varchar(71) NOT NULL,
    prior_event_hash varchar(71) NOT NULL,
    event_hash varchar(71) NOT NULL UNIQUE,
    UNIQUE (alert_id, sequence),
    CONSTRAINT research_ops_service_alert_event_sequence_valid
        CHECK (sequence > 0),
    CONSTRAINT research_ops_service_alert_event_type_valid CHECK (
        event_type IN (
            'OPENED',
            'DELIVERY_CLAIMED',
            'DELIVERED',
            'DELIVERY_FAILED',
            'ACKNOWLEDGED',
            'ESCALATED',
            'RESOLVED'
        )
    ),
    CONSTRAINT research_ops_service_alert_event_actor_valid
        CHECK (actor_id ~ '^[A-Za-z0-9][A-Za-z0-9._:-]{0,254}$'),
    CONSTRAINT research_ops_service_alert_event_reason_valid
        CHECK (reason_code ~ '^[a-z][a-z0-9_]{0,127}$'),
    CONSTRAINT research_ops_service_alert_event_details_hash_valid
        CHECK (details_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT research_ops_service_alert_event_prior_hash_valid CHECK (
        (sequence = 1 AND prior_event_hash = '')
        OR
        (sequence > 1 AND prior_event_hash ~ '^sha256:[0-9a-f]{64}$')
    ),
    CONSTRAINT research_ops_service_alert_event_hash_valid
        CHECK (event_hash ~ '^sha256:[0-9a-f]{64}$')
);

CREATE INDEX research_ops_service_alert_event_time_idx
    ON research_ops.service_alert_event(alert_id, sequence);

CREATE FUNCTION research_ops.reject_service_alert_event_mutation()
RETURNS trigger
LANGUAGE plpgsql
SET search_path = pg_catalog
AS $$
BEGIN
    RAISE EXCEPTION 'service_alert_event_append_only'
        USING ERRCODE = '55000';
END;
$$;

CREATE TRIGGER research_ops_service_alert_event_append_only
BEFORE UPDATE OR DELETE ON research_ops.service_alert_event
FOR EACH ROW
EXECUTE FUNCTION research_ops.reject_service_alert_event_mutation();
