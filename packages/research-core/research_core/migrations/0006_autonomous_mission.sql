CREATE TABLE admission_legacy_empty_guard (
    row_count INTEGER NOT NULL CHECK (row_count = 0)
);

INSERT INTO admission_legacy_empty_guard(row_count)
SELECT
    (SELECT COUNT(*) FROM admission_case_preparation) +
    (SELECT COUNT(*) FROM admission_objection) +
    (SELECT COUNT(*) FROM admission_review_binding) +
    (SELECT COUNT(*) FROM admission_decision_binding);

DROP TABLE admission_decision_binding;
DROP TABLE admission_review_binding;
DROP TABLE admission_objection;
DROP TABLE admission_case_preparation;
DROP TABLE admission_legacy_empty_guard;

CREATE TABLE admission_case (
    case_id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    candidate_revision INTEGER NOT NULL CHECK (candidate_revision >= 1),
    candidate_digest TEXT NOT NULL CHECK (length(candidate_digest) = 64),
    document_json TEXT NOT NULL,
    document_digest TEXT NOT NULL UNIQUE CHECK (length(document_digest) = 64),
    source_json TEXT NOT NULL,
    source_digest TEXT NOT NULL CHECK (length(source_digest) = 64),
    producing_session_id TEXT NOT NULL,
    producing_session_revision INTEGER NOT NULL CHECK (producing_session_revision >= 1),
    producing_attempt_id TEXT NOT NULL,
    result_digest TEXT NOT NULL CHECK (length(result_digest) = 64),
    finished_at TEXT NOT NULL,
    output_evidence_id TEXT NOT NULL,
    output_evidence_revision INTEGER NOT NULL CHECK (output_evidence_revision >= 1),
    output_evidence_digest TEXT NOT NULL CHECK (length(output_evidence_digest) = 64),
    output_closure_id TEXT NOT NULL,
    output_closure_digest TEXT NOT NULL CHECK (length(output_closure_digest) = 64),
    canonical_pre_digest TEXT NOT NULL CHECK (length(canonical_pre_digest) = 64),
    canonical_post_digest TEXT NOT NULL CHECK (length(canonical_post_digest) = 64),
    canonical_effect TEXT NOT NULL CHECK (canonical_effect = 'none'),
    created_at TEXT NOT NULL,
    FOREIGN KEY (candidate_id, candidate_revision)
        REFERENCES candidate_revision(object_id, revision),
    FOREIGN KEY (producing_session_id, producing_session_revision)
        REFERENCES session_revision(object_id, revision),
    FOREIGN KEY (producing_attempt_id) REFERENCES attempt_reference(attempt_id),
    FOREIGN KEY (output_evidence_id, output_evidence_revision)
        REFERENCES evidence_item_revision(evidence_id, revision),
    FOREIGN KEY (output_closure_id) REFERENCES closure_manifest(closure_id)
);

CREATE TABLE admission_review (
    review_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    candidate_revision INTEGER NOT NULL CHECK (candidate_revision >= 1),
    candidate_digest TEXT NOT NULL CHECK (length(candidate_digest) = 64),
    review_role TEXT NOT NULL,
    disposition TEXT NOT NULL CHECK (disposition IN ('supports', 'rejects')),
    document_json TEXT NOT NULL,
    document_digest TEXT NOT NULL UNIQUE CHECK (length(document_digest) = 64),
    source_json TEXT NOT NULL,
    source_digest TEXT NOT NULL CHECK (length(source_digest) = 64),
    producing_session_id TEXT NOT NULL,
    producing_session_revision INTEGER NOT NULL CHECK (producing_session_revision >= 1),
    producing_attempt_id TEXT NOT NULL,
    result_digest TEXT NOT NULL CHECK (length(result_digest) = 64),
    finished_at TEXT NOT NULL,
    output_evidence_id TEXT NOT NULL,
    output_evidence_revision INTEGER NOT NULL CHECK (output_evidence_revision >= 1),
    output_evidence_digest TEXT NOT NULL CHECK (length(output_evidence_digest) = 64),
    output_closure_id TEXT NOT NULL,
    output_closure_digest TEXT NOT NULL CHECK (length(output_closure_digest) = 64),
    canonical_effect TEXT NOT NULL CHECK (canonical_effect = 'none'),
    created_at TEXT NOT NULL,
    UNIQUE (case_id, review_role),
    FOREIGN KEY (case_id) REFERENCES admission_case(case_id),
    FOREIGN KEY (candidate_id, candidate_revision)
        REFERENCES candidate_revision(object_id, revision),
    FOREIGN KEY (producing_session_id, producing_session_revision)
        REFERENCES session_revision(object_id, revision),
    FOREIGN KEY (producing_attempt_id) REFERENCES attempt_reference(attempt_id),
    FOREIGN KEY (output_evidence_id, output_evidence_revision)
        REFERENCES evidence_item_revision(evidence_id, revision),
    FOREIGN KEY (output_closure_id) REFERENCES closure_manifest(closure_id)
);

CREATE TABLE admission_decision (
    decision_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    candidate_revision INTEGER NOT NULL CHECK (candidate_revision >= 1),
    candidate_digest TEXT NOT NULL CHECK (length(candidate_digest) = 64),
    disposition TEXT NOT NULL CHECK (
        disposition IN ('authorize_exact_delta', 'reject', 'blocked')
    ),
    document_json TEXT NOT NULL,
    document_digest TEXT NOT NULL UNIQUE CHECK (length(document_digest) = 64),
    source_json TEXT NOT NULL,
    source_digest TEXT NOT NULL CHECK (length(source_digest) = 64),
    producing_session_id TEXT NOT NULL,
    producing_session_revision INTEGER NOT NULL CHECK (producing_session_revision >= 1),
    producing_attempt_id TEXT NOT NULL,
    result_digest TEXT NOT NULL CHECK (length(result_digest) = 64),
    finished_at TEXT NOT NULL,
    output_evidence_id TEXT NOT NULL,
    output_evidence_revision INTEGER NOT NULL CHECK (output_evidence_revision >= 1),
    output_evidence_digest TEXT NOT NULL CHECK (length(output_evidence_digest) = 64),
    output_closure_id TEXT NOT NULL,
    output_closure_digest TEXT NOT NULL CHECK (length(output_closure_digest) = 64),
    open_a1_binding_digest TEXT CHECK (
        open_a1_binding_digest IS NULL OR length(open_a1_binding_digest) = 64
    ),
    canonical_effect TEXT NOT NULL CHECK (canonical_effect = 'none'),
    created_at TEXT NOT NULL,
    FOREIGN KEY (case_id) REFERENCES admission_case(case_id),
    FOREIGN KEY (candidate_id, candidate_revision)
        REFERENCES candidate_revision(object_id, revision),
    FOREIGN KEY (producing_session_id, producing_session_revision)
        REFERENCES session_revision(object_id, revision),
    FOREIGN KEY (producing_attempt_id) REFERENCES attempt_reference(attempt_id),
    FOREIGN KEY (output_evidence_id, output_evidence_revision)
        REFERENCES evidence_item_revision(evidence_id, revision),
    FOREIGN KEY (output_closure_id) REFERENCES closure_manifest(closure_id)
);

CREATE INDEX admission_review_case
ON admission_review(case_id, review_role, review_id);

CREATE INDEX admission_decision_case
ON admission_decision(case_id, created_at, decision_id);

CREATE TABLE admission_canonical_transaction (
    transaction_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL UNIQUE,
    decision_digest TEXT NOT NULL UNIQUE CHECK (length(decision_digest) = 64),
    case_id TEXT NOT NULL,
    candidate_id TEXT NOT NULL,
    candidate_revision INTEGER NOT NULL CHECK (candidate_revision >= 1),
    candidate_digest TEXT NOT NULL CHECK (length(candidate_digest) = 64),
    mission_id TEXT NOT NULL,
    mission_close_transition_digest TEXT NOT NULL UNIQUE CHECK (
        length(mission_close_transition_digest) = 64
    ),
    intent_json TEXT NOT NULL,
    intent_digest TEXT NOT NULL UNIQUE CHECK (length(intent_digest) = 64),
    successor_json TEXT NOT NULL,
    successor_digest TEXT NOT NULL CHECK (length(successor_digest) = 64),
    canonical_pre_digest TEXT NOT NULL CHECK (length(canonical_pre_digest) = 64),
    canonical_post_digest TEXT NOT NULL CHECK (length(canonical_post_digest) = 64),
    lifecycle TEXT NOT NULL CHECK (lifecycle IN ('pending', 'applied', 'rebound')),
    receipt_json TEXT,
    receipt_digest TEXT UNIQUE CHECK (
        receipt_digest IS NULL OR length(receipt_digest) = 64
    ),
    workspace_rebind_json TEXT,
    workspace_rebind_digest TEXT UNIQUE CHECK (
        workspace_rebind_digest IS NULL OR length(workspace_rebind_digest) = 64
    ),
    canonical_effect TEXT NOT NULL CHECK (
        canonical_effect IN ('none', 'exact_authorized_delta')
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (decision_id) REFERENCES admission_decision(decision_id),
    FOREIGN KEY (case_id) REFERENCES admission_case(case_id),
    FOREIGN KEY (candidate_id, candidate_revision)
        REFERENCES candidate_revision(object_id, revision),
    CHECK (
        (lifecycle = 'pending' AND receipt_json IS NULL AND
            receipt_digest IS NULL AND workspace_rebind_json IS NULL AND
            workspace_rebind_digest IS NULL AND canonical_effect = 'none')
        OR
        (lifecycle = 'applied' AND receipt_json IS NOT NULL AND
            receipt_digest IS NOT NULL AND workspace_rebind_json IS NULL AND
            workspace_rebind_digest IS NULL AND
            canonical_effect = 'exact_authorized_delta')
        OR
        (lifecycle = 'rebound' AND receipt_json IS NOT NULL AND
            receipt_digest IS NOT NULL AND workspace_rebind_json IS NOT NULL AND
            workspace_rebind_digest IS NOT NULL AND
            canonical_effect = 'exact_authorized_delta')
    )
);

CREATE INDEX admission_canonical_transaction_lifecycle
ON admission_canonical_transaction(lifecycle, transaction_id);

CREATE TABLE transition_journal_v6 (
    sequence_no INTEGER PRIMARY KEY CHECK (sequence_no > 0),
    project_id TEXT NOT NULL,
    project_commit_no INTEGER NOT NULL CHECK (project_commit_no > 0),
    command_id TEXT NOT NULL UNIQUE,
    command_kind TEXT NOT NULL,
    request_digest TEXT NOT NULL CHECK (length(request_digest) = 64),
    actor TEXT NOT NULL,
    writer_epoch INTEGER NOT NULL,
    changed_heads_json TEXT NOT NULL,
    auxiliary_writes_json TEXT NOT NULL,
    auxiliary_writes_digest TEXT NOT NULL CHECK (length(auxiliary_writes_digest) = 64),
    evidence_head_advances_json TEXT NOT NULL,
    evidence_head_advances_digest TEXT NOT NULL CHECK (length(evidence_head_advances_digest) = 64),
    authorization_json TEXT NOT NULL,
    canonical_effect TEXT NOT NULL CHECK (
        canonical_effect IN ('none', 'exact_authorized_delta')
    ),
    predecessor_digest TEXT,
    digest_sha256 TEXT NOT NULL UNIQUE CHECK (length(digest_sha256) = 64),
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES workspace_metadata(project_id),
    FOREIGN KEY (writer_epoch) REFERENCES writer_epoch(epoch)
);

INSERT INTO transition_journal_v6(
    sequence_no, project_id, project_commit_no, command_id, command_kind,
    request_digest, actor, writer_epoch, changed_heads_json,
    auxiliary_writes_json, auxiliary_writes_digest,
    evidence_head_advances_json, evidence_head_advances_digest,
    authorization_json, canonical_effect, predecessor_digest, digest_sha256,
    created_at
)
SELECT
    sequence_no, project_id, project_commit_no, command_id, command_kind,
    request_digest, actor, writer_epoch, changed_heads_json,
    auxiliary_writes_json, auxiliary_writes_digest,
    evidence_head_advances_json, evidence_head_advances_digest,
    authorization_json, canonical_effect, predecessor_digest, digest_sha256,
    created_at
FROM transition_journal;

DROP TABLE transition_journal;
ALTER TABLE transition_journal_v6 RENAME TO transition_journal;

CREATE TABLE session_attempt_binding_v6 (
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
        workstation_journal_schema_version IN (4, 5)
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

INSERT INTO session_attempt_binding_v6(
    binding_id, intent_id, intent_digest, project_id, mission_id, session_id,
    session_revision, attempt_id, attempt_chain_id, attempt_sequence,
    workstation_journal_schema_version, workstation_attempt_state, fence_id,
    fence_digest, coordination_id, coordination_digest, authorization_id,
    authorization_digest, prepared_record_digest, created_at
)
SELECT
    binding_id, intent_id, intent_digest, project_id, mission_id, session_id,
    session_revision, attempt_id, attempt_chain_id, attempt_sequence,
    workstation_journal_schema_version, workstation_attempt_state, fence_id,
    fence_digest, coordination_id, coordination_digest, authorization_id,
    authorization_digest, prepared_record_digest, created_at
FROM session_attempt_binding;

DROP TABLE session_attempt_binding;
ALTER TABLE session_attempt_binding_v6 RENAME TO session_attempt_binding;
