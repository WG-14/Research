ALTER TABLE research_ops.worker_heartbeat
    ADD COLUMN git_sha varchar(40) NOT NULL DEFAULT '',
    ADD COLUMN release_id varchar(128) NOT NULL DEFAULT '',
    ADD COLUMN build_digest varchar(71) NOT NULL DEFAULT '',
    ADD COLUMN release_bundle_digest varchar(71) NOT NULL DEFAULT '',
    ADD COLUMN release_seen_at timestamptz NULL;

ALTER TABLE research_ops.worker_heartbeat
    ADD CONSTRAINT research_ops_worker_release_valid CHECK (
        (
            git_sha = ''
            AND release_id = ''
            AND build_digest = ''
            AND release_bundle_digest = ''
            AND release_seen_at IS NULL
        )
        OR
        (
            git_sha ~ '^[0-9a-f]{40}$'
            AND release_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'
            AND build_digest ~ '^sha256:[0-9a-f]{64}$'
            AND release_bundle_digest ~ '^sha256:[0-9a-f]{64}$'
            AND release_seen_at IS NOT NULL
        )
    );

ALTER TABLE research_ops.backup_set
    ADD COLUMN git_sha varchar(40) NOT NULL DEFAULT '',
    ADD COLUMN build_digest varchar(71) NOT NULL DEFAULT '',
    ADD COLUMN release_bundle_digest varchar(71) NOT NULL DEFAULT '';

ALTER TABLE research_ops.backup_set
    ADD CONSTRAINT research_ops_backup_release_provenance_valid CHECK (
        (
            git_sha = ''
            AND build_digest = ''
            AND release_bundle_digest = ''
        )
        OR
        (
            git_sha ~ '^[0-9a-f]{40}$'
            AND build_digest ~ '^sha256:[0-9a-f]{64}$'
            AND release_bundle_digest ~ '^sha256:[0-9a-f]{64}$'
        )
    );
