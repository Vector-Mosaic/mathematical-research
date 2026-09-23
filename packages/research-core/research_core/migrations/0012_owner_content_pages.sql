-- Final owner-content pages are introduced directly from schema10/root6.
-- The registered 0011 DDL is part of this suffix, but no schema11 index or
-- project transition is constructed. Genesis has no workspace metadata yet.
CREATE TABLE migration_v12_guard (
    violation_count INTEGER NOT NULL CHECK (violation_count = 0)
);
INSERT INTO migration_v12_guard(violation_count)
SELECT COUNT(*) FROM workspace_metadata
WHERE schema_version != 10
   OR operating_mode != 'mission_runtime'
   OR root_digest_version != 6
   OR current_writer_epoch IS NOT NULL
   OR lifecycle NOT IN ('offline', 'quiesced');
INSERT INTO migration_v12_guard(violation_count)
SELECT COUNT(*) FROM owner_content_index_node;
DROP TABLE migration_v12_guard;

-- Preserve the historical root3/root4 and root5/root6 transition witnesses.
-- Schema11 has no retained runtime source; its ordinary index is not migrated.
CREATE TABLE workspace_root_contract_transition_v12 (
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
        (source_schema_version = 10 AND target_schema_version = 12
         AND source_root_digest_version = 6 AND target_root_digest_version = 6)
    ),
    FOREIGN KEY (writer_epoch) REFERENCES writer_epoch(epoch),
    FOREIGN KEY (migration_execution_id, migration_attempt_id)
        REFERENCES migration_execution(execution_id, attempt_id),
    FOREIGN KEY (source_project_commit) REFERENCES project_commit(commit_no)
);
INSERT INTO workspace_root_contract_transition_v12
SELECT * FROM workspace_root_contract_transition;
DROP TABLE workspace_root_contract_transition;
ALTER TABLE workspace_root_contract_transition_v12
RENAME TO workspace_root_contract_transition;

-- The same Store-owned content-addressed table now has one closed page codec.
-- Its authenticated owner validates canonical bytes, profile, scope, complete
-- keys/positions, child summaries and every qualified source binding.
DROP TABLE owner_content_index_node;
CREATE TABLE owner_content_index_node (
    node_digest TEXT PRIMARY KEY NOT NULL CHECK (length(node_digest) = 64),
    node_kind TEXT NOT NULL CHECK (node_kind IN (
        'range-cell', 'range-branch', 'range-source', 'range-position-vector'
    )),
    node_json TEXT NOT NULL
);
