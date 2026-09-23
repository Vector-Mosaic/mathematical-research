-- Schema11 adds a Store-owned authenticated access projection. The root6
-- formula and every pre-migration transition digest remain unchanged.
CREATE TABLE migration_v11_guard (
    violation_count INTEGER NOT NULL CHECK (violation_count = 0)
);
INSERT INTO migration_v11_guard(violation_count)
SELECT COUNT(*) FROM workspace_metadata
WHERE schema_version != 10
   OR operating_mode != 'mission_runtime'
   OR root_digest_version != 6
   OR current_writer_epoch IS NOT NULL
   OR lifecycle NOT IN ('offline', 'quiesced');
DROP TABLE migration_v11_guard;

-- Rebuild only the existing closed transition contract, preserving its rows.
-- The existing offline migration owner suspends/rechecks foreign keys for
-- this parent-table replacement inside its exclusive transaction.
CREATE TABLE workspace_root_contract_transition_v11 (
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
        OR
        (source_schema_version = 10 AND target_schema_version = 11
         AND source_root_digest_version = 6 AND target_root_digest_version = 6)
    ),
    FOREIGN KEY (writer_epoch) REFERENCES writer_epoch(epoch),
    FOREIGN KEY (migration_execution_id, migration_attempt_id)
        REFERENCES migration_execution(execution_id, attempt_id),
    FOREIGN KEY (source_project_commit) REFERENCES project_commit(commit_no)
);
INSERT INTO workspace_root_contract_transition_v11
SELECT * FROM workspace_root_contract_transition;
DROP TABLE workspace_root_contract_transition;
ALTER TABLE workspace_root_contract_transition_v11
RENAME TO workspace_root_contract_transition;

-- A single private content-addressed directory/posting AVL node family.
-- Exact closed codecs authenticate the canonical JSON, profile, key/value,
-- child digests, balance/height and subtree counts in the Store index owner.
CREATE TABLE owner_content_index_node (
    node_digest TEXT PRIMARY KEY NOT NULL CHECK (length(node_digest) = 64),
    node_kind TEXT NOT NULL CHECK (node_kind IN ('directory', 'posting')),
    node_json TEXT NOT NULL
);

-- NULL is retained only for pre-schema11 journals with their original bytes.
-- Every schema11 transition must seal the closed descriptor in its digest;
-- the Store journal validator owns this boundary-dependent requirement.
ALTER TABLE transition_journal ADD COLUMN owner_content_index_json TEXT;

-- Dynamic root Capture metadata changes only for exact affected captures.
-- Seek capture origins in the changed checkpoint interval without scanning
-- all retained Captures; metadata bodies remain with their custody owner.
CREATE INDEX transition_journal_capture_commit
ON transition_journal(
    project_commit_no,
    json_extract(auxiliary_writes_json, '$[#-1].primary_key.capture_id')
)
WHERE command_kind = 'commit_raw_capture';
