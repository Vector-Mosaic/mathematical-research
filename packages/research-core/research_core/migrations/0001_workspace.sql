CREATE TABLE schema_migration (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    name TEXT NOT NULL UNIQUE,
    digest_sha256 TEXT NOT NULL CHECK (length(digest_sha256) = 64),
    applied_at TEXT NOT NULL
);

CREATE TABLE workspace_metadata (
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
    updated_at TEXT NOT NULL
);

CREATE TABLE writer_epoch (
    epoch INTEGER PRIMARY KEY CHECK (epoch > 0),
    project_id TEXT NOT NULL,
    owner TEXT NOT NULL,
    lifecycle TEXT NOT NULL CHECK (lifecycle IN (
        'active', 'draining', 'quiesced', 'revoked', 'superseded'
    )),
    creation_basis TEXT NOT NULL,
    predecessor_epoch INTEGER,
    takeover_evidence_json TEXT,
    created_at TEXT NOT NULL,
    closed_at TEXT,
    row_digest TEXT NOT NULL CHECK (length(row_digest) = 64),
    FOREIGN KEY (project_id) REFERENCES workspace_metadata(project_id),
    FOREIGN KEY (predecessor_epoch) REFERENCES writer_epoch(epoch)
);

CREATE UNIQUE INDEX one_active_writer_epoch
    ON writer_epoch(project_id) WHERE lifecycle = 'active';

CREATE TABLE project_commit (
    commit_no INTEGER PRIMARY KEY CHECK (commit_no >= 0),
    project_id TEXT NOT NULL,
    root_digest TEXT NOT NULL CHECK (length(root_digest) = 64),
    canonical_authority_digest TEXT NOT NULL CHECK (length(canonical_authority_digest) = 64),
    transition_head_digest TEXT,
    command_id TEXT,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES workspace_metadata(project_id)
);

CREATE TABLE mission_bundle_snapshot (
    project_commit_no INTEGER PRIMARY KEY CHECK (project_commit_no > 0),
    project_id TEXT NOT NULL,
    bundle_digest TEXT NOT NULL CHECK (length(bundle_digest) = 64),
    bundle_json TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    created_at TEXT NOT NULL,
    row_digest TEXT NOT NULL CHECK (length(row_digest) = 64),
    FOREIGN KEY (project_commit_no) REFERENCES project_commit(commit_no),
    FOREIGN KEY (project_id) REFERENCES workspace_metadata(project_id)
);

CREATE TABLE transition_journal (
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
    canonical_effect TEXT NOT NULL CHECK (canonical_effect = 'none'),
    predecessor_digest TEXT,
    digest_sha256 TEXT NOT NULL UNIQUE CHECK (length(digest_sha256) = 64),
    created_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES workspace_metadata(project_id),
    FOREIGN KEY (writer_epoch) REFERENCES writer_epoch(epoch)
);

CREATE TABLE command_result (
    command_id TEXT PRIMARY KEY,
    actor TEXT NOT NULL,
    request_digest TEXT NOT NULL CHECK (length(request_digest) = 64),
    writer_epoch INTEGER NOT NULL,
    result_digest TEXT NOT NULL CHECK (length(result_digest) = 64),
    result_json TEXT NOT NULL,
    project_commit_no INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (writer_epoch) REFERENCES writer_epoch(epoch),
    FOREIGN KEY (project_commit_no) REFERENCES project_commit(commit_no)
);

CREATE TABLE mission_revision (
    object_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL CHECK (length(payload_digest) = 64),
    predecessor_revision INTEGER,
    created_actor TEXT NOT NULL,
    created_session_id TEXT,
    created_evidence_id TEXT,
    authorization_json TEXT NOT NULL,
    terminal_history_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    row_digest TEXT NOT NULL CHECK (length(row_digest) = 64),
    PRIMARY KEY (object_id, revision),
    FOREIGN KEY (object_id, predecessor_revision)
        REFERENCES mission_revision(object_id, revision)
);
CREATE TABLE mission_head (
    object_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    payload_digest TEXT NOT NULL CHECK (length(payload_digest) = 64),
    project_commit_no INTEGER NOT NULL,
    FOREIGN KEY (object_id, revision) REFERENCES mission_revision(object_id, revision),
    FOREIGN KEY (project_commit_no) REFERENCES project_commit(commit_no)
);

CREATE TABLE branch_revision (
    object_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL CHECK (length(payload_digest) = 64),
    predecessor_revision INTEGER,
    created_actor TEXT NOT NULL,
    created_session_id TEXT,
    created_evidence_id TEXT,
    authorization_json TEXT NOT NULL,
    terminal_history_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    row_digest TEXT NOT NULL CHECK (length(row_digest) = 64),
    PRIMARY KEY (object_id, revision),
    FOREIGN KEY (object_id, predecessor_revision)
        REFERENCES branch_revision(object_id, revision)
);
CREATE TABLE branch_head (
    object_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    payload_digest TEXT NOT NULL CHECK (length(payload_digest) = 64),
    project_commit_no INTEGER NOT NULL,
    FOREIGN KEY (object_id, revision) REFERENCES branch_revision(object_id, revision),
    FOREIGN KEY (project_commit_no) REFERENCES project_commit(commit_no)
);

CREATE TABLE strategy_revision (
    object_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL CHECK (length(payload_digest) = 64),
    predecessor_revision INTEGER,
    created_actor TEXT NOT NULL,
    created_session_id TEXT,
    created_evidence_id TEXT,
    authorization_json TEXT NOT NULL,
    terminal_history_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    row_digest TEXT NOT NULL CHECK (length(row_digest) = 64),
    PRIMARY KEY (object_id, revision),
    FOREIGN KEY (object_id, predecessor_revision)
        REFERENCES strategy_revision(object_id, revision)
);
CREATE TABLE strategy_head (
    object_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    payload_digest TEXT NOT NULL CHECK (length(payload_digest) = 64),
    project_commit_no INTEGER NOT NULL,
    FOREIGN KEY (object_id, revision) REFERENCES strategy_revision(object_id, revision),
    FOREIGN KEY (project_commit_no) REFERENCES project_commit(commit_no)
);

CREATE TABLE context_revision (
    object_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL CHECK (length(payload_digest) = 64),
    predecessor_revision INTEGER,
    created_actor TEXT NOT NULL,
    created_session_id TEXT,
    created_evidence_id TEXT,
    authorization_json TEXT NOT NULL,
    terminal_history_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    row_digest TEXT NOT NULL CHECK (length(row_digest) = 64),
    PRIMARY KEY (object_id, revision),
    FOREIGN KEY (object_id, predecessor_revision)
        REFERENCES context_revision(object_id, revision)
);
CREATE TABLE context_head (
    object_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    payload_digest TEXT NOT NULL CHECK (length(payload_digest) = 64),
    project_commit_no INTEGER NOT NULL,
    FOREIGN KEY (object_id, revision) REFERENCES context_revision(object_id, revision),
    FOREIGN KEY (project_commit_no) REFERENCES project_commit(commit_no)
);

CREATE TABLE session_revision (
    object_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL CHECK (length(payload_digest) = 64),
    predecessor_revision INTEGER,
    created_actor TEXT NOT NULL,
    created_session_id TEXT,
    created_evidence_id TEXT,
    authorization_json TEXT NOT NULL,
    terminal_history_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    row_digest TEXT NOT NULL CHECK (length(row_digest) = 64),
    PRIMARY KEY (object_id, revision),
    FOREIGN KEY (object_id, predecessor_revision)
        REFERENCES session_revision(object_id, revision)
);
CREATE TABLE session_head (
    object_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    payload_digest TEXT NOT NULL CHECK (length(payload_digest) = 64),
    project_commit_no INTEGER NOT NULL,
    FOREIGN KEY (object_id, revision) REFERENCES session_revision(object_id, revision),
    FOREIGN KEY (project_commit_no) REFERENCES project_commit(commit_no)
);

CREATE TABLE candidate_revision (
    object_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    payload_json TEXT NOT NULL,
    payload_digest TEXT NOT NULL CHECK (length(payload_digest) = 64),
    predecessor_revision INTEGER,
    created_actor TEXT NOT NULL,
    created_session_id TEXT,
    created_evidence_id TEXT,
    authorization_json TEXT NOT NULL,
    terminal_history_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    row_digest TEXT NOT NULL CHECK (length(row_digest) = 64),
    PRIMARY KEY (object_id, revision),
    FOREIGN KEY (object_id, predecessor_revision)
        REFERENCES candidate_revision(object_id, revision)
);
CREATE TABLE candidate_head (
    object_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    payload_digest TEXT NOT NULL CHECK (length(payload_digest) = 64),
    project_commit_no INTEGER NOT NULL,
    FOREIGN KEY (object_id, revision) REFERENCES candidate_revision(object_id, revision),
    FOREIGN KEY (project_commit_no) REFERENCES project_commit(commit_no)
);

CREATE TABLE blob (
    sha256 TEXT PRIMARY KEY CHECK (length(sha256) = 64),
    byte_length INTEGER NOT NULL CHECK (byte_length >= 0),
    media_type TEXT NOT NULL,
    encoding TEXT,
    integrity_state TEXT NOT NULL,
    availability_state TEXT NOT NULL,
    logical_cas_path TEXT NOT NULL UNIQUE,
    first_verified_at TEXT,
    last_verified_at TEXT,
    quarantine_state TEXT NOT NULL
);

CREATE TABLE evidence_item_revision (
    evidence_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK (revision > 0),
    subtype TEXT NOT NULL,
    subject_json TEXT NOT NULL,
    exact_scope_json TEXT NOT NULL,
    rigor TEXT NOT NULL,
    limitations_json TEXT NOT NULL,
    non_inferences_json TEXT NOT NULL,
    security_json TEXT NOT NULL,
    retention_json TEXT NOT NULL,
    availability_state TEXT NOT NULL CHECK (availability_state IN (
        'pending_closure', 'verified_available', 'unavailable'
    )),
    canonical_effect TEXT NOT NULL CHECK (canonical_effect = 'none'),
    payload_digest TEXT NOT NULL CHECK (length(payload_digest) = 64),
    created_at TEXT NOT NULL,
    PRIMARY KEY (evidence_id, revision)
);
CREATE TABLE evidence_item_head (
    evidence_id TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    payload_digest TEXT NOT NULL CHECK (length(payload_digest) = 64),
    project_commit_no INTEGER NOT NULL,
    FOREIGN KEY (evidence_id, revision)
        REFERENCES evidence_item_revision(evidence_id, revision),
    FOREIGN KEY (project_commit_no) REFERENCES project_commit(commit_no)
);
CREATE TABLE evidence_blob (
    evidence_id TEXT NOT NULL,
    evidence_revision INTEGER NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    role TEXT NOT NULL,
    blob_sha256 TEXT NOT NULL,
    PRIMARY KEY (evidence_id, evidence_revision, ordinal),
    FOREIGN KEY (evidence_id, evidence_revision)
        REFERENCES evidence_item_revision(evidence_id, revision),
    FOREIGN KEY (blob_sha256) REFERENCES blob(sha256)
);
CREATE TABLE provenance_event (
    provenance_id TEXT PRIMARY KEY,
    evidence_id TEXT NOT NULL,
    evidence_revision INTEGER NOT NULL,
    origin_json TEXT NOT NULL,
    activity_json TEXT NOT NULL,
    agent_json TEXT NOT NULL,
    tool_json TEXT NOT NULL,
    input_json TEXT NOT NULL,
    transformation_json TEXT NOT NULL,
    custody_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (evidence_id, evidence_revision)
        REFERENCES evidence_item_revision(evidence_id, revision)
);
CREATE TABLE independence_disclosure (
    disclosure_id TEXT PRIMARY KEY,
    evidence_id TEXT NOT NULL,
    evidence_revision INTEGER NOT NULL,
    model_json TEXT NOT NULL,
    exposure_json TEXT NOT NULL,
    method_json TEXT NOT NULL,
    sources_json TEXT NOT NULL,
    implementation_json TEXT NOT NULL,
    environment_json TEXT NOT NULL,
    correlation_json TEXT NOT NULL,
    FOREIGN KEY (evidence_id, evidence_revision)
        REFERENCES evidence_item_revision(evidence_id, revision)
);
CREATE TABLE evidence_relation (
    relation_id TEXT PRIMARY KEY,
    source_evidence_id TEXT NOT NULL,
    source_revision INTEGER NOT NULL,
    target_evidence_id TEXT NOT NULL,
    target_revision INTEGER NOT NULL,
    relation_kind TEXT NOT NULL CHECK (relation_kind IN (
        'successor', 'correction', 'reproduction', 'review', 'duplicate',
        'dispute', 'consumer_use'
    )),
    scope_json TEXT NOT NULL,
    FOREIGN KEY (source_evidence_id, source_revision)
        REFERENCES evidence_item_revision(evidence_id, revision),
    FOREIGN KEY (target_evidence_id, target_revision)
        REFERENCES evidence_item_revision(evidence_id, revision)
);
CREATE TABLE closure_manifest (
    closure_id TEXT PRIMARY KEY,
    root_kind TEXT NOT NULL,
    root_digest TEXT NOT NULL CHECK (length(root_digest) = 64),
    members_json TEXT NOT NULL,
    contract_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE deletion_directive (
    directive_id TEXT PRIMARY KEY,
    authorization_json TEXT NOT NULL,
    exact_scope_json TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE evidence_tombstone (
    tombstone_id TEXT PRIMARY KEY,
    evidence_id TEXT NOT NULL,
    evidence_revision INTEGER NOT NULL,
    directive_id TEXT NOT NULL,
    reason_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (evidence_id, evidence_revision)
        REFERENCES evidence_item_revision(evidence_id, revision),
    FOREIGN KEY (directive_id) REFERENCES deletion_directive(directive_id)
);

CREATE TABLE reservation (
    reservation_id TEXT PRIMARY KEY,
    mission_id TEXT NOT NULL,
    mission_revision INTEGER NOT NULL,
    session_id TEXT,
    ceiling_json TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    command_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (mission_id, mission_revision)
        REFERENCES mission_revision(object_id, revision)
);
CREATE TABLE reservation_revision (
    reservation_id TEXT NOT NULL,
    project_commit_no INTEGER NOT NULL,
    mission_id TEXT NOT NULL,
    mission_revision INTEGER NOT NULL,
    session_id TEXT,
    ceiling_json TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    command_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    row_digest TEXT NOT NULL CHECK(length(row_digest) = 64),
    PRIMARY KEY (reservation_id, project_commit_no),
    FOREIGN KEY (project_commit_no) REFERENCES project_commit(commit_no),
    FOREIGN KEY (mission_id, mission_revision)
        REFERENCES mission_revision(object_id, revision)
);
CREATE TABLE session_settlement (
    settlement_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    session_revision INTEGER NOT NULL,
    attempt_references_json TEXT NOT NULL,
    charge_basis_json TEXT NOT NULL,
    release_conversion_json TEXT NOT NULL,
    command_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id, session_revision)
        REFERENCES session_revision(object_id, revision)
);
CREATE TABLE attempt_reference (
    attempt_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    session_revision INTEGER NOT NULL,
    intent_digest TEXT NOT NULL CHECK (length(intent_digest) = 64),
    result_digest TEXT,
    observed_lifecycle TEXT NOT NULL,
    settlement_state TEXT NOT NULL,
    unknown_effect INTEGER NOT NULL CHECK (unknown_effect IN (0, 1)),
    FOREIGN KEY (session_id, session_revision)
        REFERENCES session_revision(object_id, revision)
);
CREATE TABLE outbox_intent (
    intent_id TEXT PRIMARY KEY,
    session_id TEXT NOT NULL,
    session_revision INTEGER NOT NULL,
    envelope_digest TEXT NOT NULL CHECK (length(envelope_digest) = 64),
    envelope_json TEXT NOT NULL,
    lifecycle TEXT NOT NULL CHECK (lifecycle = 'inactive'),
    created_at TEXT NOT NULL,
    FOREIGN KEY (session_id, session_revision)
        REFERENCES session_revision(object_id, revision)
);
CREATE TABLE inbox_receipt (
    receipt_id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL,
    envelope_digest TEXT NOT NULL CHECK (length(envelope_digest) = 64),
    envelope_json TEXT NOT NULL,
    lifecycle TEXT NOT NULL CHECK (lifecycle = 'inactive'),
    created_at TEXT NOT NULL,
    FOREIGN KEY (attempt_id) REFERENCES attempt_reference(attempt_id)
);

CREATE TABLE alert_event (
    alert_id TEXT PRIMARY KEY,
    severity TEXT NOT NULL CHECK (severity IN ('A1', 'A2', 'B', 'C', 'D')),
    summary_json TEXT NOT NULL,
    proof_neutral INTEGER NOT NULL CHECK (proof_neutral = 1),
    created_at TEXT NOT NULL
);
CREATE TABLE alert_scope (
    alert_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
    scope_kind TEXT NOT NULL,
    scope_id TEXT NOT NULL,
    scope_revision INTEGER,
    PRIMARY KEY (alert_id, ordinal),
    FOREIGN KEY (alert_id) REFERENCES alert_event(alert_id)
);
CREATE TABLE alert_delivery_intent (
    delivery_intent_id TEXT PRIMARY KEY,
    alert_id TEXT NOT NULL,
    transport_kind TEXT NOT NULL,
    lifecycle TEXT NOT NULL CHECK (lifecycle = 'inactive'),
    created_at TEXT NOT NULL,
    FOREIGN KEY (alert_id) REFERENCES alert_event(alert_id)
);
CREATE TABLE hold (
    hold_id TEXT PRIMARY KEY,
    alert_id TEXT NOT NULL,
    exact_scope_json TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (alert_id) REFERENCES alert_event(alert_id)
);
CREATE TABLE hold_resolution (
    resolution_id TEXT PRIMARY KEY,
    hold_id TEXT NOT NULL,
    authorization_json TEXT NOT NULL,
    resolution_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY (hold_id) REFERENCES hold(hold_id)
);

CREATE TABLE admission_case_preparation (
    case_id TEXT PRIMARY KEY,
    subject_json TEXT NOT NULL,
    evidence_closure_digest TEXT NOT NULL CHECK (length(evidence_closure_digest) = 64),
    delta_preview_digest TEXT NOT NULL CHECK (length(delta_preview_digest) = 64),
    delta_preview_json TEXT NOT NULL,
    lifecycle TEXT NOT NULL,
    canonical_effect TEXT NOT NULL CHECK (canonical_effect = 'none'),
    created_at TEXT NOT NULL
);
CREATE TABLE admission_objection (
    objection_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    objection_json TEXT NOT NULL,
    evidence_digest TEXT,
    created_at TEXT NOT NULL,
    FOREIGN KEY (case_id) REFERENCES admission_case_preparation(case_id)
);
CREATE TABLE admission_review_binding (
    review_binding_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    reviewer_json TEXT NOT NULL,
    evidence_digest TEXT NOT NULL CHECK (length(evidence_digest) = 64),
    created_at TEXT NOT NULL,
    FOREIGN KEY (case_id) REFERENCES admission_case_preparation(case_id)
);
CREATE TABLE admission_decision_binding (
    decision_binding_id TEXT PRIMARY KEY,
    case_id TEXT NOT NULL,
    canonical_pre_digest TEXT NOT NULL CHECK (length(canonical_pre_digest) = 64),
    canonical_post_digest TEXT NOT NULL CHECK (length(canonical_post_digest) = 64),
    incumbent_decision_digest TEXT NOT NULL CHECK (length(incumbent_decision_digest) = 64),
    projection_only INTEGER NOT NULL CHECK (projection_only = 1),
    created_at TEXT NOT NULL,
    FOREIGN KEY (case_id) REFERENCES admission_case_preparation(case_id)
);
