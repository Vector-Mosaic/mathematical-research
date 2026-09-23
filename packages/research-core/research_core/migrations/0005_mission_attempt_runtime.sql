CREATE TABLE workspace_metadata_v5 (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    project_id TEXT NOT NULL UNIQUE,
    application_version TEXT NOT NULL,
    schema_version INTEGER NOT NULL CHECK (schema_version > 0),
    root_identity TEXT NOT NULL CHECK (length(root_identity) = 64),
    lifecycle TEXT NOT NULL CHECK (lifecycle IN (
        'offline', 'recovering', 'verified_read_only', 'takeover_pending',
        'active', 'draining', 'quiesced', 'blocked_read_only'
    )),
    canonical_authority_json TEXT NOT NULL,
    canonical_authority_digest TEXT NOT NULL CHECK (length(canonical_authority_digest) = 64),
    current_writer_epoch INTEGER,
    current_project_commit INTEGER NOT NULL DEFAULT 0 CHECK (current_project_commit >= 0),
    current_root_digest TEXT NOT NULL CHECK (length(current_root_digest) = 64),
    transition_head_digest TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    operating_mode TEXT NOT NULL DEFAULT 'inactive_foundation' CHECK (
        operating_mode IN (
            'inactive_foundation',
            'pre_cutover_observer',
            'mission_runtime'
        )
    ),
    root_digest_version INTEGER NOT NULL DEFAULT 1 CHECK (
        root_digest_version IN (1, 2, 3)
    )
);

INSERT INTO workspace_metadata_v5(
    singleton, project_id, application_version, schema_version, root_identity,
    lifecycle, canonical_authority_json, canonical_authority_digest,
    current_writer_epoch, current_project_commit, current_root_digest,
    transition_head_digest, created_at, updated_at, operating_mode,
    root_digest_version
)
SELECT
    singleton, project_id, application_version, schema_version, root_identity,
    lifecycle, canonical_authority_json, canonical_authority_digest,
    current_writer_epoch, current_project_commit, current_root_digest,
    transition_head_digest, created_at, updated_at, operating_mode,
    root_digest_version
FROM workspace_metadata;

DROP TABLE workspace_metadata;
ALTER TABLE workspace_metadata_v5 RENAME TO workspace_metadata;

CREATE TABLE outbox_intent_v5 (
    intent_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    session_revision INTEGER NOT NULL,
    envelope_digest TEXT NOT NULL CHECK (length(envelope_digest) = 64),
    envelope_json TEXT NOT NULL,
    lifecycle TEXT NOT NULL CHECK (lifecycle IN ('inactive', 'pending')),
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id, session_revision)
        REFERENCES session_revision(object_id, revision)
);

INSERT INTO outbox_intent_v5(
    intent_id, session_id, session_revision, envelope_digest,
    envelope_json, lifecycle, created_at
)
SELECT
    intent_id, session_id, session_revision, envelope_digest,
    envelope_json, lifecycle, created_at
FROM outbox_intent;

DROP TABLE outbox_intent;
ALTER TABLE outbox_intent_v5 RENAME TO outbox_intent;

CREATE TABLE inbox_receipt_v5 (
    receipt_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL,
    envelope_digest TEXT NOT NULL CHECK (length(envelope_digest) = 64),
    envelope_json TEXT NOT NULL,
    lifecycle TEXT NOT NULL CHECK (lifecycle IN ('inactive', 'received')),
    created_at TEXT NOT NULL
);

INSERT INTO inbox_receipt_v5(
    receipt_id, attempt_id, envelope_digest, envelope_json, lifecycle, created_at
)
SELECT
    receipt_id, attempt_id, envelope_digest, envelope_json, lifecycle, created_at
FROM inbox_receipt;

DROP TABLE inbox_receipt;
ALTER TABLE inbox_receipt_v5 RENAME TO inbox_receipt;

CREATE INDEX inbox_receipt_attempt
ON inbox_receipt(attempt_id, created_at, receipt_id);

CREATE TABLE session_attempt_binding (
    binding_id TEXT PRIMARY KEY,
    intent_id TEXT NOT NULL UNIQUE,
    intent_digest TEXT NOT NULL CHECK (length(intent_digest) = 64),
    project_id TEXT NOT NULL,
    mission_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    session_revision INTEGER NOT NULL,
    attempt_id TEXT NOT NULL UNIQUE,
    attempt_chain_id TEXT NOT NULL,
    attempt_sequence INTEGER NOT NULL CHECK (attempt_sequence >= 1),
    workstation_journal_schema_version INTEGER NOT NULL CHECK (
        workstation_journal_schema_version = 4
    ),
    workstation_attempt_state TEXT NOT NULL CHECK (
        workstation_attempt_state = 'prepared'
    ),
    fence_id TEXT NOT NULL,
    fence_digest TEXT NOT NULL CHECK (length(fence_digest) = 64),
    coordination_id TEXT NOT NULL,
    coordination_digest TEXT NOT NULL CHECK (length(coordination_digest) = 64),
    authorization_id TEXT NOT NULL,
    authorization_digest TEXT NOT NULL CHECK (length(authorization_digest) = 64),
    prepared_record_digest TEXT NOT NULL CHECK (length(prepared_record_digest) = 64),
    created_at TEXT NOT NULL,
    FOREIGN KEY (intent_id) REFERENCES outbox_intent(intent_id),
    FOREIGN KEY (session_id, session_revision)
        REFERENCES session_revision(object_id, revision)
);

CREATE TABLE attempt_evidence_binding (
    binding_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL UNIQUE,
    result_receipt_id TEXT NOT NULL UNIQUE,
    result_digest TEXT NOT NULL CHECK (length(result_digest) = 64),
    evidence_id TEXT NOT NULL,
    evidence_revision INTEGER NOT NULL CHECK (evidence_revision >= 1),
    evidence_payload_digest TEXT NOT NULL CHECK (length(evidence_payload_digest) = 64),
    closure_id TEXT NOT NULL,
    closure_digest TEXT NOT NULL CHECK (length(closure_digest) = 64),
    created_at TEXT NOT NULL,
    FOREIGN KEY (attempt_id) REFERENCES attempt_reference(attempt_id),
    FOREIGN KEY (result_receipt_id) REFERENCES inbox_receipt(receipt_id),
    FOREIGN KEY (evidence_id, evidence_revision)
        REFERENCES evidence_item_revision(evidence_id, revision),
    FOREIGN KEY (closure_id) REFERENCES closure_manifest(closure_id)
);

CREATE TABLE session_settlement_v5 (
    settlement_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    session_revision INTEGER NOT NULL,
    attempt_references_json TEXT NOT NULL,
    charge_basis_json TEXT NOT NULL,
    release_conversion_json TEXT NOT NULL,
    evidence_binding_id TEXT,
    command_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id, session_revision)
        REFERENCES session_revision(object_id, revision),
    FOREIGN KEY (evidence_binding_id)
        REFERENCES attempt_evidence_binding(binding_id)
);

INSERT INTO session_settlement_v5(
    settlement_id, session_id, session_revision, attempt_references_json,
    charge_basis_json, release_conversion_json, evidence_binding_id,
    command_id, created_at
)
SELECT
    settlement_id, session_id, session_revision, attempt_references_json,
    charge_basis_json, release_conversion_json, NULL,
    command_id, created_at
FROM session_settlement;

DROP TABLE session_settlement;
ALTER TABLE session_settlement_v5 RENAME TO session_settlement;
