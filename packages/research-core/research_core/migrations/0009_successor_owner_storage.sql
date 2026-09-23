-- Schema 9 establishes the successor physical owner cut.  Existing semantic
-- revision/head tables remain the sole Mission, Branch, Strategy, Context,
-- Session, and Candidate stores.  These seven tables add only immutable
-- capture custody, Evidence source location, revisable capture-scope facts,
-- append-only Executive Epoch events, and immutable continuation checkpoints.

CREATE TABLE migration_v9_guard (
    violation_count INTEGER NOT NULL CHECK (violation_count = 0)
);

-- A populated source must be the exact inactive schema8/root4 Mission image
-- consumed by the specialized dot6 successor upgrader.  Fresh genesis has no
-- metadata row and therefore also satisfies the guard.
INSERT INTO migration_v9_guard(violation_count)
SELECT COUNT(*)
FROM workspace_metadata
WHERE schema_version != 8
   OR operating_mode != 'mission_runtime'
   OR root_digest_version != 4
   OR current_writer_epoch IS NOT NULL;

DROP TABLE migration_v9_guard;

CREATE TABLE workspace_metadata_v9 (
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
        root_digest_version IN (1, 2, 3, 4, 5)
    )
);

INSERT INTO workspace_metadata_v9(
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
ALTER TABLE workspace_metadata_v9 RENAME TO workspace_metadata;

-- Raw capture may preserve sealed, opaque output that is not eligible for
-- ordinary Evidence reads.  Keep the custody reason with the Blob metadata;
-- schema-8 could not commit quarantined Blobs, so every carried row remains
-- NULL here until a successor raw-capture write supplies a reason.
ALTER TABLE blob ADD COLUMN quarantine_reason TEXT;

CREATE TABLE executive_epoch_event (
    executive_epoch_id TEXT NOT NULL,
    event_ordinal INTEGER NOT NULL CHECK (event_ordinal > 0),
    mission_id TEXT NOT NULL,
    project_commit_no INTEGER NOT NULL,
    event_kind TEXT NOT NULL CHECK (event_kind IN (
        'authorized', 'bound', 'checkpointed', 'failed_before_checkpoint'
    )),
    event_json TEXT NOT NULL,
    event_digest TEXT NOT NULL CHECK (length(event_digest) = 64),
    predecessor_event_ordinal INTEGER,
    predecessor_event_digest TEXT,
    created_actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    row_digest TEXT NOT NULL CHECK (length(row_digest) = 64),
    PRIMARY KEY (executive_epoch_id, event_ordinal),
    UNIQUE (executive_epoch_id, event_ordinal, event_digest),
    FOREIGN KEY (
        executive_epoch_id,
        predecessor_event_ordinal,
        predecessor_event_digest
    ) REFERENCES executive_epoch_event(
        executive_epoch_id,
        event_ordinal,
        event_digest
    ),
    FOREIGN KEY (project_commit_no) REFERENCES project_commit(commit_no),
    CHECK (
        (
            event_ordinal = 1
            AND predecessor_event_ordinal IS NULL
            AND predecessor_event_digest IS NULL
        )
        OR (
            event_ordinal > 1
            AND predecessor_event_ordinal = event_ordinal - 1
            AND predecessor_event_digest IS NOT NULL
        )
    )
);

CREATE INDEX executive_epoch_event_mission_order
ON executive_epoch_event(mission_id, executive_epoch_id, event_ordinal);

CREATE UNIQUE INDEX executive_epoch_event_one_bound
ON executive_epoch_event(executive_epoch_id)
WHERE event_kind = 'bound';

CREATE UNIQUE INDEX executive_epoch_event_one_terminal
ON executive_epoch_event(executive_epoch_id)
WHERE event_kind IN ('checkpointed', 'failed_before_checkpoint');

CREATE TABLE continuation_checkpoint (
    checkpoint_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    executive_epoch_id TEXT NOT NULL UNIQUE,
    project_commit_no INTEGER NOT NULL,
    document_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL CHECK (length(payload_digest) = 64),
    created_actor TEXT NOT NULL,
    created_at TEXT NOT NULL,
    row_digest TEXT NOT NULL CHECK (length(row_digest) = 64),
    FOREIGN KEY (project_commit_no) REFERENCES project_commit(commit_no)
);

CREATE INDEX continuation_checkpoint_mission
ON continuation_checkpoint(mission_id, checkpoint_id);

CREATE TABLE raw_capture (
    capture_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    mission_id TEXT NOT NULL,
    executive_epoch_id TEXT,
    capture_kind TEXT NOT NULL CHECK (capture_kind IN ('assignment', 'output')),
    observation_id TEXT NOT NULL,
    assignment_id TEXT NOT NULL,
    provenance_json TEXT NOT NULL,
    completion_json TEXT,
    created_at TEXT NOT NULL,
    row_digest TEXT NOT NULL CHECK (length(row_digest) = 64),
    UNIQUE (project_id, capture_kind, observation_id),
    FOREIGN KEY (project_id) REFERENCES workspace_metadata(project_id),
    CHECK (capture_kind = 'output' OR completion_json IS NULL)
);

CREATE INDEX raw_capture_mission_epoch_order
ON raw_capture(mission_id, executive_epoch_id, created_at, capture_id);

CREATE TABLE raw_capture_artifact (
    capture_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    role TEXT NOT NULL,
    logical_name TEXT NOT NULL,
    blob_sha256 TEXT NOT NULL,
    PRIMARY KEY (capture_id, ordinal),
    FOREIGN KEY (capture_id) REFERENCES raw_capture(capture_id),
    FOREIGN KEY (blob_sha256) REFERENCES blob(sha256)
);

CREATE INDEX raw_capture_artifact_blob
ON raw_capture_artifact(blob_sha256, capture_id, ordinal);

CREATE TABLE evidence_capture_source (
    evidence_id TEXT NOT NULL,
    evidence_revision INTEGER NOT NULL CHECK (evidence_revision > 0),
    source_ordinal INTEGER NOT NULL CHECK (source_ordinal >= 0),
    capture_id TEXT NOT NULL,
    artifact_ordinal INTEGER NOT NULL CHECK (artifact_ordinal >= 0),
    exact_scope_json TEXT NOT NULL,
    PRIMARY KEY (evidence_id, evidence_revision, source_ordinal),
    FOREIGN KEY (evidence_id, evidence_revision)
        REFERENCES evidence_item_revision(evidence_id, revision),
    FOREIGN KEY (capture_id, artifact_ordinal)
        REFERENCES raw_capture_artifact(capture_id, ordinal)
);

CREATE INDEX evidence_capture_source_capture_scope
ON evidence_capture_source(
    capture_id, artifact_ordinal, evidence_id, evidence_revision, source_ordinal
);

CREATE TABLE capture_scope_annotation_revision (
    annotation_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    capture_id TEXT NOT NULL,
    annotation_kind TEXT NOT NULL CHECK (
        annotation_kind = 'reviewed-no-current-semantic-delta'
    ),
    exact_scope_json TEXT NOT NULL,
    lifecycle TEXT NOT NULL CHECK (lifecycle IN ('active', 'removed')),
    predecessor_revision INTEGER,
    created_actor TEXT NOT NULL,
    created_epoch_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    payload_digest TEXT NOT NULL CHECK (length(payload_digest) = 64),
    row_digest TEXT NOT NULL CHECK (length(row_digest) = 64),
    PRIMARY KEY (annotation_id, revision),
    UNIQUE (annotation_id, revision, capture_id),
    UNIQUE (annotation_id, revision, payload_digest),
    FOREIGN KEY (capture_id) REFERENCES raw_capture(capture_id),
    FOREIGN KEY (annotation_id, predecessor_revision, capture_id)
        REFERENCES capture_scope_annotation_revision(
            annotation_id, revision, capture_id
        ),
    CHECK (
        (revision = 1 AND predecessor_revision IS NULL)
        OR (revision > 1 AND predecessor_revision = revision - 1)
    )
);

CREATE INDEX capture_scope_annotation_capture_revision
ON capture_scope_annotation_revision(capture_id, annotation_id, revision);

CREATE TABLE capture_scope_annotation_head (
    annotation_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL CHECK (revision > 0),
    payload_digest TEXT NOT NULL CHECK (length(payload_digest) = 64),
    project_commit_no INTEGER NOT NULL,
    FOREIGN KEY (annotation_id, revision, payload_digest)
        REFERENCES capture_scope_annotation_revision(
            annotation_id, revision, payload_digest
        ),
    FOREIGN KEY (project_commit_no) REFERENCES project_commit(commit_no)
);
