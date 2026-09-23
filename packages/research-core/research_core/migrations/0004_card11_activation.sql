ALTER TABLE workspace_metadata
ADD COLUMN operating_mode TEXT NOT NULL DEFAULT 'inactive_foundation'
CHECK (operating_mode IN ('inactive_foundation', 'pre_cutover_observer'));

ALTER TABLE workspace_metadata
ADD COLUMN root_digest_version INTEGER NOT NULL DEFAULT 1
CHECK (root_digest_version IN (1, 2));

CREATE TABLE card11_activation_record (
    operation_id TEXT NOT NULL,
    record_kind TEXT NOT NULL CHECK (record_kind IN (
        'genesis', 'source_quiesced', 'failed'
    )),
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL CHECK (length(payload_digest) = 64),
    created_at TEXT NOT NULL,
    row_digest TEXT NOT NULL CHECK (length(row_digest) = 64),
    PRIMARY KEY (operation_id, record_kind)
);

CREATE TABLE card11_activation_binding (
    operation_id TEXT PRIMARY KEY,
    project_commit_no INTEGER NOT NULL UNIQUE,
    activation_plan_digest TEXT NOT NULL CHECK (length(activation_plan_digest) = 64),
    observation_digest TEXT NOT NULL CHECK (length(observation_digest) = 64),
    closure_id TEXT NOT NULL,
    closure_digest TEXT NOT NULL CHECK (length(closure_digest) = 64),
    import_manifest_digest TEXT NOT NULL CHECK (length(import_manifest_digest) = 64),
    source_blob_sha256 TEXT NOT NULL CHECK (length(source_blob_sha256) = 64),
    canonical_effect TEXT NOT NULL CHECK (canonical_effect = 'none'),
    coordination_effect TEXT NOT NULL CHECK (coordination_effect = 'none'),
    mathematical_effect TEXT NOT NULL CHECK (mathematical_effect = 'none'),
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_commit_no) REFERENCES project_commit(commit_no),
    FOREIGN KEY (closure_id) REFERENCES closure_manifest(closure_id),
    FOREIGN KEY (source_blob_sha256) REFERENCES blob(sha256)
);

CREATE TABLE card11_campaign_observation (
    operation_id TEXT PRIMARY KEY,
    compatibility_version INTEGER NOT NULL CHECK (compatibility_version = 2),
    source_path TEXT NOT NULL,
    source_commit TEXT NOT NULL CHECK (length(source_commit) = 40),
    campaign_sha256 TEXT NOT NULL CHECK (length(campaign_sha256) = 64),
    campaign_schema_version INTEGER NOT NULL CHECK (campaign_schema_version = 4),
    campaign_status TEXT NOT NULL,
    protocol_path TEXT NOT NULL,
    protocol_sha256 TEXT NOT NULL CHECK (length(protocol_sha256) = 64),
    canonical_authority_digest TEXT NOT NULL CHECK (length(canonical_authority_digest) = 64),
    project_ref TEXT NOT NULL,
    observation_json TEXT NOT NULL,
    observation_digest TEXT NOT NULL UNIQUE CHECK (length(observation_digest) = 64),
    closure_id TEXT NOT NULL,
    source_blob_sha256 TEXT NOT NULL CHECK (length(source_blob_sha256) = 64),
    canonical_effect TEXT NOT NULL CHECK (canonical_effect = 'none'),
    coordination_effect TEXT NOT NULL CHECK (coordination_effect = 'none'),
    mathematical_effect TEXT NOT NULL CHECK (mathematical_effect = 'none'),
    created_at TEXT NOT NULL,
    FOREIGN KEY (operation_id) REFERENCES card11_activation_binding(operation_id),
    FOREIGN KEY (closure_id) REFERENCES closure_manifest(closure_id),
    FOREIGN KEY (source_blob_sha256) REFERENCES blob(sha256)
);

CREATE TABLE card11_capability_consumption (
    nonce TEXT PRIMARY KEY,
    operation_id TEXT NOT NULL,
    capability_kind TEXT NOT NULL CHECK (capability_kind IN (
        'provisioning', 'claim_writer', 'activate_campaign_observer',
        'release_writer', 'fail_card11_activation'
    )),
    capability_digest TEXT NOT NULL UNIQUE CHECK (length(capability_digest) = 64),
    command_id TEXT,
    writer_epoch INTEGER,
    consumed_at TEXT NOT NULL,
    row_digest TEXT NOT NULL CHECK (length(row_digest) = 64)
);
