CREATE TABLE IF NOT EXISTS research_ops.research_job_result_receipt (
    job_id uuid PRIMARY KEY,
    authority varchar(128) NOT NULL,
    experiment_id varchar(255) NOT NULL,
    request_id varchar(255) NOT NULL,
    request_hash varchar(71) NOT NULL,
    admission_run_id uuid NOT NULL,
    fencing_token bigint NOT NULL,
    result_ref varchar(1024) NOT NULL,
    result_hash varchar(71) NOT NULL,
    research_outcome varchar(16) NOT NULL,
    core_run_id varchar(128) NOT NULL DEFAULT '',
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    applied_at timestamptz NULL,
    FOREIGN KEY (authority, experiment_id, request_id)
        REFERENCES research_ops.experiment_request(authority, experiment_id, request_id)
        ON DELETE RESTRICT,
    CONSTRAINT research_ops_job_receipt_request_hash_valid
        CHECK (request_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT research_ops_job_receipt_fencing_valid
        CHECK (fencing_token > 0),
    CONSTRAINT research_ops_job_receipt_result_hash_valid
        CHECK (result_hash ~ '^sha256:[0-9a-f]{64}$'),
    CONSTRAINT research_ops_job_receipt_outcome_valid
        CHECK (research_outcome IN ('PASS', 'FAIL')),
    UNIQUE (authority, experiment_id, request_id)
);

CREATE INDEX IF NOT EXISTS research_ops_job_receipt_unapplied_idx
    ON research_ops.research_job_result_receipt(created_at)
    WHERE applied_at IS NULL;
