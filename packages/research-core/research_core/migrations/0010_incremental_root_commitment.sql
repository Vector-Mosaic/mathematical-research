-- Schema10 keeps the existing root-contract transition owner and permits
-- its exact offline schema9/root5 -> schema10/root6 transition. The ordinary
-- current commitment binds one transition; no retained mathematical row changes.

CREATE TABLE migration_v10_guard (
    violation_count INTEGER NOT NULL CHECK (violation_count = 0)
);
INSERT INTO migration_v10_guard(violation_count)
SELECT COUNT(*) FROM workspace_metadata
WHERE schema_version != 9
   OR operating_mode != 'mission_runtime'
   OR root_digest_version != 5
   OR current_writer_epoch IS NOT NULL
   OR lifecycle NOT IN ('offline', 'quiesced');
DROP TABLE migration_v10_guard;

CREATE TABLE workspace_metadata_v10 (
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
        root_digest_version IN (1, 2, 3, 4, 5, 6)
    )
);

INSERT INTO workspace_metadata_v10(
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
ALTER TABLE workspace_metadata_v10 RENAME TO workspace_metadata;

CREATE TABLE workspace_root_contract_transition_v10 (
    transition_id TEXT PRIMARY KEY,
    command_id TEXT NOT NULL UNIQUE,
    writer_epoch INTEGER NOT NULL,
    migration_execution_id TEXT NOT NULL UNIQUE,
    migration_attempt_id TEXT NOT NULL UNIQUE,
    plan_sha256 TEXT NOT NULL CHECK (length(plan_sha256) = 64),
    verified_backup_manifest_sha256 TEXT NOT NULL CHECK (
        length(verified_backup_manifest_sha256) = 64
    ),
    source_project_commit INTEGER NOT NULL CHECK (source_project_commit >= 0),
    source_root_digest TEXT NOT NULL CHECK (length(source_root_digest) = 64),
    source_transition_head_digest TEXT CHECK (
        source_transition_head_digest IS NULL OR
        length(source_transition_head_digest) = 64
    ),
    canonical_authority_digest TEXT NOT NULL CHECK (
        length(canonical_authority_digest) = 64
    ),
    source_schema_version INTEGER NOT NULL,
    target_schema_version INTEGER NOT NULL UNIQUE,
    source_root_digest_version INTEGER NOT NULL,
    target_root_digest_version INTEGER NOT NULL,
    target_migration_set_digest TEXT NOT NULL CHECK (
        length(target_migration_set_digest) = 64
    ),
    target_schema_object_digest TEXT NOT NULL CHECK (
        length(target_schema_object_digest) = 64
    ),
    canonical_effect TEXT NOT NULL CHECK (canonical_effect = 'none'),
    created_at TEXT NOT NULL,
    row_digest TEXT NOT NULL CHECK (length(row_digest) = 64),
    CHECK (
        (source_schema_version = 6 AND target_schema_version = 7
         AND source_root_digest_version = 3 AND target_root_digest_version = 4)
        OR
        (source_schema_version = 9 AND target_schema_version = 10
         AND source_root_digest_version = 5 AND target_root_digest_version = 6)
    ),
    FOREIGN KEY (writer_epoch) REFERENCES writer_epoch(epoch),
    FOREIGN KEY (migration_execution_id, migration_attempt_id)
        REFERENCES migration_execution(execution_id, attempt_id),
    FOREIGN KEY (source_project_commit) REFERENCES project_commit(commit_no)
);

INSERT INTO workspace_root_contract_transition_v10
SELECT * FROM workspace_root_contract_transition;
DROP TABLE workspace_root_contract_transition;
ALTER TABLE workspace_root_contract_transition_v10
RENAME TO workspace_root_contract_transition;

-- Immutable membership projection of the exact typed heads current at the
-- verified 9->10 source cut. Its sole authority is the root/count in the
-- existing transition-journal digest; it is not a historical revision set.
CREATE TABLE workspace_root_transition_retained_head (
    transition_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    object_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    source_project_commit INTEGER NOT NULL CHECK (source_project_commit >= 0),
    payload_digest TEXT NOT NULL CHECK (length(payload_digest) = 64),
    revision_row_digest TEXT NOT NULL CHECK (length(revision_row_digest) = 64),
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    proof_json TEXT NOT NULL,
    PRIMARY KEY (transition_id, kind, object_id),
    UNIQUE (transition_id, ordinal),
    FOREIGN KEY (transition_id)
        REFERENCES workspace_root_contract_transition(transition_id)
);

-- Root6 binds exact new typed-row digests through the existing transition
-- journal. Historical journal rows retain NULL and their original digest body.
ALTER TABLE transition_journal ADD COLUMN changed_head_rows_json TEXT;

-- Root6 binds the command's semantic result separately from the persisted
-- replay envelope.  The envelope also contains the resulting transition/root
-- digests, so binding its complete bytes would be circular.  Historical
-- pre-root6 journals remain NULL and retain their original digest meaning.
ALTER TABLE transition_journal
ADD COLUMN result_payload_digest TEXT CHECK (
    result_payload_digest IS NULL OR length(result_payload_digest) = 64
);

-- A revision-local pointer binds each native root6 Evidence row to its one
-- existing transition-journal origin.  This is not a second journal or
-- historical witness: the pointed-to journal envelope remains the authority,
-- and root6 commits the pointer as part of the Evidence row itself.  Migrated
-- root5 rows remain NULL and retain their explicit exhaustive compatibility
-- validation path under their original row meaning.
ALTER TABLE evidence_item_revision
ADD COLUMN project_commit_no INTEGER
    CHECK (project_commit_no IS NULL OR project_commit_no > 0)
    REFERENCES project_commit(commit_no);

-- One private, closed current-dependency projection supports authenticated
-- positive and negative lookups without turning routine work into a retained-
-- history scan.  It is an AVL search tree over only the Store-owned dependency
-- families named below.  Its current root/count ride in the existing journal;
-- neither table is a journal, semantic authority, or historical witness.
CREATE TABLE current_dependency_node (
    scope_key TEXT PRIMARY KEY CHECK (length(scope_key) = 64),
    family TEXT NOT NULL CHECK (family IN (
        'a2_scope',
        'security_evidence',
        'security_blob',
        'security_tombstone',
        'candidate_a1',
        'candidate_a1_mission',
        'mission_continuity',
        'inherited_current_head'
    )),
    scope_json TEXT NOT NULL,
    state_json TEXT NOT NULL,
    state_digest TEXT NOT NULL CHECK (length(state_digest) = 64),
    left_scope_key TEXT,
    left_digest TEXT,
    left_height INTEGER NOT NULL CHECK (
        typeof(left_height) = 'integer' AND left_height >= 0
    ),
    right_scope_key TEXT,
    right_digest TEXT,
    right_height INTEGER NOT NULL CHECK (
        typeof(right_height) = 'integer' AND right_height >= 0
    ),
    height INTEGER NOT NULL CHECK (
        typeof(height) = 'integer' AND height > 0
    ),
    subtree_digest TEXT NOT NULL CHECK (length(subtree_digest) = 64),
    CHECK (
        (left_scope_key IS NULL AND left_digest IS NULL AND left_height = 0)
        OR
        (length(left_scope_key) = 64 AND length(left_digest) = 64 AND left_height > 0)
    ),
    CHECK (
        (right_scope_key IS NULL AND right_digest IS NULL AND right_height = 0)
        OR
        (length(right_scope_key) = 64 AND length(right_digest) = 64 AND right_height > 0)
    ),
    CHECK (left_scope_key IS NULL OR left_scope_key != scope_key),
    CHECK (right_scope_key IS NULL OR right_scope_key != scope_key)
);

CREATE TABLE current_dependency_projection (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    project_id TEXT NOT NULL,
    current_project_commit INTEGER NOT NULL CHECK (
        typeof(current_project_commit) = 'integer' AND current_project_commit >= 0
    ),
    root_scope_key TEXT CHECK (
        root_scope_key IS NULL OR length(root_scope_key) = 64
    ),
    root_digest TEXT NOT NULL CHECK (length(root_digest) = 64),
    entry_count INTEGER NOT NULL CHECK (
        typeof(entry_count) = 'integer' AND entry_count >= 0
    ),
    FOREIGN KEY (project_id) REFERENCES workspace_metadata(project_id)
);

ALTER TABLE transition_journal
ADD COLUMN current_dependency_root_digest TEXT CHECK (
    current_dependency_root_digest IS NULL
    OR length(current_dependency_root_digest) = 64
);
ALTER TABLE transition_journal
ADD COLUMN current_dependency_entry_count INTEGER CHECK (
    current_dependency_entry_count IS NULL
    OR (
        typeof(current_dependency_entry_count) = 'integer'
        AND current_dependency_entry_count >= 0
    )
);

-- One immutable, structurally shared authenticated dictionary preserves the
-- exact current coverer count for every retained Capture artifact at every
-- native root6 project-commit cut.  Nodes are content-addressed and append-only;
-- the existing transition journal owns each cut's root and exact cardinality.
-- This is a derived custody projection, not a semantic owner or second journal.
CREATE TABLE capture_artifact_coverage_node (
    node_digest TEXT PRIMARY KEY CHECK (length(node_digest) = 64),
    scope_key TEXT NOT NULL CHECK (length(scope_key) = 64),
    mission_id TEXT NOT NULL,
    capture_id TEXT NOT NULL,
    artifact_ordinal INTEGER NOT NULL CHECK (
        typeof(artifact_ordinal) = 'integer' AND artifact_ordinal >= 0
    ),
    coverer_count INTEGER NOT NULL CHECK (
        typeof(coverer_count) = 'integer' AND coverer_count >= 0
    ),
    left_node_digest TEXT CHECK (
        left_node_digest IS NULL OR length(left_node_digest) = 64
    ),
    left_height INTEGER NOT NULL CHECK (
        typeof(left_height) = 'integer' AND left_height >= 0
    ),
    left_entry_count INTEGER NOT NULL CHECK (
        typeof(left_entry_count) = 'integer' AND left_entry_count >= 0
    ),
    right_node_digest TEXT CHECK (
        right_node_digest IS NULL OR length(right_node_digest) = 64
    ),
    right_height INTEGER NOT NULL CHECK (
        typeof(right_height) = 'integer' AND right_height >= 0
    ),
    right_entry_count INTEGER NOT NULL CHECK (
        typeof(right_entry_count) = 'integer' AND right_entry_count >= 0
    ),
    height INTEGER NOT NULL CHECK (
        typeof(height) = 'integer' AND height > 0
    ),
    subtree_entry_count INTEGER NOT NULL CHECK (
        typeof(subtree_entry_count) = 'integer' AND subtree_entry_count > 0
    ),
    CHECK (
        (left_node_digest IS NULL AND left_height = 0 AND left_entry_count = 0)
        OR
        (left_node_digest IS NOT NULL AND left_height > 0 AND left_entry_count > 0)
    ),
    CHECK (
        (right_node_digest IS NULL AND right_height = 0 AND right_entry_count = 0)
        OR
        (right_node_digest IS NOT NULL AND right_height > 0 AND right_entry_count > 0)
    )
);

-- Immutable, revision-local Merkle search nodes let ordinary root hook
-- pagination resume inside one authored owner without reparsing that owner's
-- complete JSON document on every transport page.  Each fixed-size family
-- root/count summary is committed by the owner's existing root6 changed-head
-- transition; the authenticated stateless cursor only transports that bound
-- selector.  These rows are therefore a disposable/rebuildable read index,
-- not a second semantic owner, journal, lifecycle, or query-session cache.
CREATE TABLE root_hook_index_node (
    node_digest TEXT PRIMARY KEY CHECK (length(node_digest) = 64),
    project_id TEXT NOT NULL,
    owner_kind TEXT NOT NULL CHECK (owner_kind IN (
        'mission', 'branch', 'strategy', 'context', 'session', 'candidate'
    )),
    owner_identity TEXT NOT NULL,
    owner_revision INTEGER NOT NULL CHECK (
        typeof(owner_revision) = 'integer' AND owner_revision >= 1
    ),
    owner_payload_digest TEXT NOT NULL CHECK (
        length(owner_payload_digest) = 64
    ),
    index_family TEXT NOT NULL CHECK (index_family IN (
        'hook_item', 'strategy_connection'
    )),
    key_json TEXT NOT NULL,
    value_digest TEXT NOT NULL CHECK (length(value_digest) = 64),
    left_node_digest TEXT CHECK (
        left_node_digest IS NULL OR length(left_node_digest) = 64
    ),
    left_height INTEGER NOT NULL CHECK (
        typeof(left_height) = 'integer' AND left_height >= 0
    ),
    left_entry_count INTEGER NOT NULL CHECK (
        typeof(left_entry_count) = 'integer' AND left_entry_count >= 0
    ),
    right_node_digest TEXT CHECK (
        right_node_digest IS NULL OR length(right_node_digest) = 64
    ),
    right_height INTEGER NOT NULL CHECK (
        typeof(right_height) = 'integer' AND right_height >= 0
    ),
    right_entry_count INTEGER NOT NULL CHECK (
        typeof(right_entry_count) = 'integer' AND right_entry_count >= 0
    ),
    height INTEGER NOT NULL CHECK (
        typeof(height) = 'integer' AND height > 0
    ),
    subtree_entry_count INTEGER NOT NULL CHECK (
        typeof(subtree_entry_count) = 'integer' AND subtree_entry_count > 0
    ),
    value_json TEXT NOT NULL,
    CHECK (
        (left_node_digest IS NULL AND left_height = 0 AND left_entry_count = 0)
        OR
        (left_node_digest IS NOT NULL AND left_height > 0 AND left_entry_count > 0)
    ),
    CHECK (
        (right_node_digest IS NULL AND right_height = 0 AND right_entry_count = 0)
        OR
        (right_node_digest IS NOT NULL AND right_height > 0 AND right_entry_count > 0)
    )
);

ALTER TABLE transition_journal
ADD COLUMN capture_coverage_root_digest TEXT CHECK (
    capture_coverage_root_digest IS NULL
    OR length(capture_coverage_root_digest) = 64
);
ALTER TABLE transition_journal
ADD COLUMN capture_coverage_entry_count INTEGER CHECK (
    capture_coverage_entry_count IS NULL
    OR (
        typeof(capture_coverage_entry_count) = 'integer'
        AND capture_coverage_entry_count >= 0
    )
);

CREATE UNIQUE INDEX transition_journal_project_commit
ON transition_journal(project_commit_no);
CREATE INDEX transition_journal_canonical_target
ON transition_journal(
    command_kind,
    json_extract(authorization_json, '$.target_canonical_authority_digest'),
    sequence_no
);
CREATE INDEX transition_journal_capture_id
ON transition_journal(
    json_extract(auxiliary_writes_json, '$[#-1].primary_key.capture_id')
)
WHERE command_kind = 'commit_raw_capture';
CREATE INDEX transition_journal_candidate_revision
ON transition_journal(changed_heads_json, project_commit_no)
WHERE command_kind = 'commit_candidate_revision';
CREATE INDEX transition_journal_single_changed_head
ON transition_journal(
    json_extract(changed_heads_json, '$[0]'),
    project_commit_no
)
WHERE json_array_length(changed_heads_json) = 1;
-- Auxiliary rows receive the transition timestamp in the same transaction.
-- This index lets an exact selected Evidence row recover the small set of
-- candidate owner transitions without assuming that its closure advanced only
-- one Evidence head or materializing a second historical-origin registry.
CREATE INDEX transition_journal_auxiliary_created_at
ON transition_journal(created_at, project_commit_no);
CREATE INDEX transition_journal_annotation_revision
ON transition_journal(
    json_extract(auxiliary_writes_json, '$[0].primary_key.annotation_id'),
    json_extract(auxiliary_writes_json, '$[0].primary_key.revision'),
    project_commit_no
)
WHERE command_kind = 'commit_capture_scope_annotation_revision';
CREATE INDEX transition_journal_evidence_head
ON transition_journal(
    json_extract(evidence_head_advances_json, '$[0].evidence_id'),
    project_commit_no
)
WHERE json_array_length(evidence_head_advances_json) = 1;
CREATE UNIQUE INDEX command_result_project_commit
ON command_result(project_commit_no);
CREATE INDEX migration_execution_target_schema
ON migration_execution(target_schema_version, execution_id);
CREATE UNIQUE INDEX workspace_root_transition_retained_head_identity
ON workspace_root_transition_retained_head(kind, object_id, revision);
CREATE INDEX branch_revision_history_seek
ON branch_revision(object_id || '@' || CAST(revision AS TEXT));
CREATE INDEX candidate_revision_history_seek
ON candidate_revision(object_id || '@' || CAST(revision AS TEXT));
CREATE INDEX context_revision_history_seek
ON context_revision(object_id || '@' || CAST(revision AS TEXT));
CREATE INDEX strategy_revision_history_seek
ON strategy_revision(object_id || '@' || CAST(revision AS TEXT));
CREATE INDEX evidence_revision_history_seek
ON evidence_item_revision(evidence_id || '@' || CAST(revision AS TEXT));
CREATE INDEX capture_annotation_revision_history_seek
ON capture_scope_annotation_revision(annotation_id || '@' || CAST(revision AS TEXT));
CREATE INDEX raw_capture_artifact_history_seek
ON raw_capture_artifact(capture_id || '#' || CAST(ordinal AS TEXT));
CREATE INDEX continuation_checkpoint_mission_commit
ON continuation_checkpoint(mission_id, project_commit_no, checkpoint_id);
CREATE INDEX executive_epoch_event_mission_boundary
ON executive_epoch_event(
    mission_id,
    project_commit_no DESC,
    event_ordinal DESC,
    executive_epoch_id,
    event_kind
);
CREATE INDEX raw_capture_mission_capture
ON raw_capture(mission_id, capture_id);
-- SQLite appends rowid to this non-unique index.  Keeping capture_id out of the
-- declared key lets native root Capture pagination seek the exact Mission and
-- bounded rowid interval in rowid order without sorting or rescanning the
-- Mission's retained Capture population on every continuation page.
CREATE INDEX raw_capture_mission_rowid_seek
ON raw_capture(mission_id);

-- Candidate A1 reads select the exact canonical artifact through its existing
-- Evidence subject. The index routes lookup; Evidence and closure checks prove it.
CREATE INDEX evidence_revision_candidate_a1_artifact
ON evidence_item_revision(
    json_extract(subject_json, '$.candidate_id'),
    json_extract(subject_json, '$.artifact_digest'),
    evidence_id,
    revision
)
WHERE subtype = 'purported_complete_route';
CREATE INDEX candidate_revision_payload
ON candidate_revision(object_id, payload_digest, revision);
CREATE INDEX hold_open_alert
ON hold(lifecycle, alert_id, hold_id);
CREATE INDEX current_dependency_candidate_a1_mission
ON current_dependency_node(
    family,
    json_extract(scope_json, '$.mission_id'),
    scope_key
)
WHERE family = 'candidate_a1';
CREATE INDEX hold_resolution_hold
ON hold_resolution(hold_id, resolution_id);

-- A closure must honor dispositions of any Evidence sharing its exact Blobs.
CREATE INDEX evidence_blob_shared_blob
ON evidence_blob(blob_sha256, evidence_id, evidence_revision);
CREATE INDEX evidence_tombstone_evidence
ON evidence_tombstone(evidence_id, evidence_revision, tombstone_id);

CREATE INDEX closure_manifest_evidence_root
ON closure_manifest(
    json_extract(contract_json, '$.root.target_id'),
    json_extract(contract_json, '$.root.target_revision'),
    closure_id
)
WHERE json_extract(contract_json, '$.root.relation') = 'evidence'
  AND json_extract(contract_json, '$.root.target_kind') = 'evidence';
CREATE INDEX provenance_event_evidence_revision
ON provenance_event(evidence_id, evidence_revision, provenance_id);
CREATE INDEX independence_disclosure_evidence_revision
ON independence_disclosure(evidence_id, evidence_revision, disclosure_id);
