CREATE TABLE migration_execution (
    execution_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL UNIQUE,
    source_schema_version INTEGER NOT NULL CHECK(source_schema_version >= 1),
    target_schema_version INTEGER NOT NULL CHECK(target_schema_version > source_schema_version),
    verified_backup_manifest_sha256 TEXT NOT NULL CHECK(length(verified_backup_manifest_sha256) = 64),
    applied_history_sha256 TEXT NOT NULL CHECK(length(applied_history_sha256) = 64),
    plan_sha256 TEXT NOT NULL CHECK(length(plan_sha256) = 64),
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status = 'applied'),
    canonical_effect TEXT NOT NULL CHECK(canonical_effect = 'none')
);

CREATE TABLE alert_acknowledgement (
    acknowledgement_id TEXT PRIMARY KEY,
    alert_id TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    command_request_digest TEXT NOT NULL CHECK(length(command_request_digest) = 64),
    created_at TEXT NOT NULL,
    FOREIGN KEY (alert_id) REFERENCES alert_event(alert_id)
);
