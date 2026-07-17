CREATE TABLE IF NOT EXISTS research_ops.runtime_control (
    singleton_id smallint PRIMARY KEY DEFAULT 1,
    mutation_admission_open boolean NOT NULL,
    claim_admission_open boolean NOT NULL,
    integrity_quarantine boolean NOT NULL,
    generation bigint NOT NULL,
    fence_token uuid NULL,
    requested_by varchar(255) NOT NULL DEFAULT '',
    reason varchar(255) NOT NULL DEFAULT '',
    closed_at timestamptz NULL,
    reopened_at timestamptz NULL,
    changed_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    last_verified_manifest_hash varchar(71) NOT NULL DEFAULT '',
    CONSTRAINT research_ops_runtime_control_singleton
        CHECK (singleton_id = 1),
    CONSTRAINT research_ops_runtime_control_generation
        CHECK (generation >= 0),
    CONSTRAINT research_ops_runtime_control_admission_order
        CHECK (NOT mutation_admission_open OR claim_admission_open),
    CONSTRAINT research_ops_runtime_control_state CHECK (
        (
            mutation_admission_open
            AND claim_admission_open
            AND NOT integrity_quarantine
            AND fence_token IS NULL
        )
        OR
        (
            NOT mutation_admission_open
            AND fence_token IS NOT NULL
            AND requested_by <> ''
            AND reason <> ''
            AND closed_at IS NOT NULL
            AND (NOT integrity_quarantine OR NOT claim_admission_open)
        )
    ),
    CONSTRAINT research_ops_runtime_control_manifest_hash CHECK (
        last_verified_manifest_hash = ''
        OR last_verified_manifest_hash ~ '^sha256:[0-9a-f]{64}$'
    )
);

INSERT INTO research_ops.runtime_control (
    singleton_id,
    mutation_admission_open,
    claim_admission_open,
    integrity_quarantine,
    generation
) VALUES (1, true, true, false, 0)
ON CONFLICT (singleton_id) DO NOTHING;

CREATE TABLE IF NOT EXISTS research_ops.validation_observation (
    kind varchar(64) PRIMARY KEY,
    status varchar(16) NOT NULL,
    reason_code varchar(128) NOT NULL,
    reason_count integer NOT NULL,
    observed_at timestamptz NOT NULL,
    evidence_hash varchar(71) NOT NULL DEFAULT '',
    row_count bigint NOT NULL DEFAULT 0,
    terminal_hash varchar(71) NOT NULL DEFAULT '',
    CONSTRAINT research_ops_validation_kind_valid
        CHECK (kind ~ '^[A-Z][A-Z0-9_]{0,63}$'),
    CONSTRAINT research_ops_validation_status_valid
        CHECK (status IN ('PASS', 'FAIL', 'STALE')),
    CONSTRAINT research_ops_validation_reason_code_valid
        CHECK (reason_code ~ '^[a-z][a-z0-9_]{0,127}$'),
    CONSTRAINT research_ops_validation_reason_count_valid
        CHECK (reason_count >= 0),
    CONSTRAINT research_ops_validation_row_count_valid
        CHECK (row_count >= 0),
    CONSTRAINT research_ops_validation_evidence_hash_valid CHECK (
        evidence_hash = '' OR evidence_hash ~ '^sha256:[0-9a-f]{64}$'
    ),
    CONSTRAINT research_ops_validation_terminal_hash_valid CHECK (
        terminal_hash = '' OR terminal_hash ~ '^sha256:[0-9a-f]{64}$'
    )
);

CREATE TABLE IF NOT EXISTS research_ops.backup_set (
    backup_id uuid PRIMARY KEY,
    manifest_hash varchar(71) NOT NULL UNIQUE,
    fence_token uuid NOT NULL,
    fence_generation bigint NOT NULL,
    release_id varchar(128) NOT NULL,
    created_at timestamptz NOT NULL,
    verified_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT research_ops_backup_manifest_hash_valid
        CHECK (manifest_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT research_ops_backup_fence_generation_valid
        CHECK (fence_generation > 0),
    CONSTRAINT research_ops_backup_release_id_valid
        CHECK (release_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$')
);

CREATE INDEX IF NOT EXISTS research_ops_backup_verified_at_idx
    ON research_ops.backup_set(verified_at DESC);

CREATE TABLE IF NOT EXISTS research_ops.restore_drill (
    drill_id uuid PRIMARY KEY,
    backup_manifest_hash varchar(71) NOT NULL,
    receipt_hash varchar(71) NOT NULL UNIQUE,
    status varchar(16) NOT NULL,
    duration_seconds double precision NOT NULL,
    finished_at timestamptz NOT NULL,
    CONSTRAINT research_ops_restore_backup_hash_valid
        CHECK (backup_manifest_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT research_ops_restore_receipt_hash_valid
        CHECK (receipt_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT research_ops_restore_status_valid
        CHECK (status IN ('PASS', 'FAIL')),
    CONSTRAINT research_ops_restore_duration_valid
        CHECK (duration_seconds >= 0 AND duration_seconds <= 604800)
);

CREATE INDEX IF NOT EXISTS research_ops_restore_finished_at_idx
    ON research_ops.restore_drill(finished_at DESC);
