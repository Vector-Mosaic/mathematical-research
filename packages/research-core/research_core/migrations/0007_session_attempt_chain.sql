-- Schema 7 replaces the singular result projection with an immutable Attempt
-- identity parent plus append-only observation and disposition chains.  Every
-- legacy fact is copied exactly; no v7 allocation, receipt, retry directive,
-- observation order, or authority is synthesized by migration.

CREATE TABLE migration_v7_guard (
    violation_count INTEGER NOT NULL CHECK (violation_count = 0)
);

-- A populated v6 source must be the exact quiesced Mission-runtime/root-v3
-- image consumed by the Store-owned offline transition.  Fresh genesis has no
-- metadata row yet and therefore also satisfies this guard.
INSERT INTO migration_v7_guard(violation_count)
SELECT COUNT(*)
FROM workspace_metadata
WHERE schema_version != 6
   OR operating_mode != 'mission_runtime'
   OR root_digest_version != 3
   OR current_writer_epoch IS NOT NULL;

-- One immutable Session intent and one final settlement remain singular per
-- Session revision.  Plural operational Attempts share that one intent.
INSERT INTO migration_v7_guard(violation_count)
SELECT COUNT(*) FROM (
    SELECT session_id, session_revision
    FROM outbox_intent
    GROUP BY session_id, session_revision
    HAVING COUNT(*) > 1
);

INSERT INTO migration_v7_guard(violation_count)
SELECT COUNT(*) FROM (
    SELECT session_id, session_revision
    FROM session_settlement
    GROUP BY session_id, session_revision
    HAVING COUNT(*) > 1
);

-- The v6 binding contract was singular.  Migration never invents retry
-- lineage for a row that already claims a later sequence.
INSERT INTO migration_v7_guard(violation_count)
SELECT COUNT(*)
FROM session_attempt_binding
WHERE attempt_sequence != 1;

INSERT INTO migration_v7_guard(violation_count)
SELECT COUNT(*) FROM (
    SELECT attempt_chain_id, attempt_sequence
    FROM session_attempt_binding
    GROUP BY attempt_chain_id, attempt_sequence
    HAVING COUNT(*) > 1
);

-- Every v6 prepared binding must converge on its exact persisted intent and
-- Session.  A result projection may be absent and is backfilled only from this
-- already-durable identity.
INSERT INTO migration_v7_guard(violation_count)
SELECT COUNT(*)
FROM session_attempt_binding AS binding
LEFT JOIN outbox_intent AS intent
  ON intent.intent_id = binding.intent_id
WHERE intent.intent_id IS NULL
   OR intent.session_id != binding.session_id
   OR intent.session_revision != binding.session_revision
   OR intent.envelope_digest != binding.intent_digest;

INSERT INTO migration_v7_guard(violation_count)
SELECT COUNT(*)
FROM attempt_reference AS reference
JOIN session_attempt_binding AS binding
  ON binding.attempt_id = reference.attempt_id
WHERE reference.session_id != binding.session_id
   OR reference.session_revision != binding.session_revision
   OR reference.intent_digest != binding.intent_digest
   OR reference.result_digest IS NULL;

-- v5/v6 deliberately removed the inbox FK while the Attempt identity was
-- assembled.  Refuse orphan observations or Evidence instead of laundering
-- them into the new chain.
INSERT INTO migration_v7_guard(violation_count)
SELECT COUNT(*)
FROM inbox_receipt AS receipt
WHERE NOT EXISTS (
    SELECT 1 FROM attempt_reference AS reference
    WHERE reference.attempt_id = receipt.attempt_id
)
AND NOT EXISTS (
    SELECT 1 FROM session_attempt_binding AS binding
    WHERE binding.attempt_id = receipt.attempt_id
);

INSERT INTO migration_v7_guard(violation_count)
SELECT COUNT(*)
FROM attempt_evidence_binding AS evidence
LEFT JOIN inbox_receipt AS receipt
  ON receipt.receipt_id = evidence.result_receipt_id
WHERE receipt.receipt_id IS NULL
   OR receipt.attempt_id != evidence.attempt_id
   OR receipt.envelope_digest != evidence.result_digest;

DROP TABLE migration_v7_guard;

CREATE TABLE workspace_metadata_v7 (
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
        root_digest_version IN (1, 2, 3, 4)
    )
);

INSERT INTO workspace_metadata_v7(
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
ALTER TABLE workspace_metadata_v7 RENAME TO workspace_metadata;

CREATE UNIQUE INDEX one_outbox_intent_per_session_revision
ON outbox_intent(session_id, session_revision);

CREATE UNIQUE INDEX outbox_intent_exact_scope
ON outbox_intent(intent_id, session_id, session_revision);

CREATE UNIQUE INDEX migration_execution_exact_attempt
ON migration_execution(execution_id, attempt_id);

CREATE TABLE session_attempt_allocation (
    allocation_id TEXT PRIMARY KEY,
    allocation_contract_version INTEGER NOT NULL CHECK (
        allocation_contract_version = 1
    ),
    intent_id TEXT NOT NULL UNIQUE,
    session_id TEXT NOT NULL,
    session_revision INTEGER NOT NULL CHECK (session_revision >= 1),
    planned_session_digest TEXT NOT NULL CHECK (length(planned_session_digest) = 64),
    attempt_policy_json TEXT NOT NULL,
    attempt_policy_sha256 TEXT NOT NULL CHECK (length(attempt_policy_sha256) = 64),
    authorization_id TEXT NOT NULL,
    authorization_sha256 TEXT NOT NULL CHECK (length(authorization_sha256) = 64),
    reservation_vector_json TEXT NOT NULL,
    reservation_vector_sha256 TEXT NOT NULL CHECK (length(reservation_vector_sha256) = 64),
    resource_envelope_json TEXT NOT NULL,
    resource_envelope_sha256 TEXT NOT NULL CHECK (length(resource_envelope_sha256) = 64),
    resource_enforcement_json TEXT NOT NULL,
    resource_enforcement_sha256 TEXT NOT NULL CHECK (length(resource_enforcement_sha256) = 64),
    allocation_sha256 TEXT NOT NULL UNIQUE CHECK (length(allocation_sha256) = 64),
    created_at TEXT NOT NULL,
    row_digest TEXT NOT NULL CHECK (length(row_digest) = 64),
    UNIQUE (session_id, session_revision),
    UNIQUE (allocation_id, intent_id, session_id, session_revision),
    UNIQUE (allocation_id, session_id, session_revision),
    FOREIGN KEY (intent_id, session_id, session_revision)
        REFERENCES outbox_intent(intent_id, session_id, session_revision),
    FOREIGN KEY (session_id, session_revision)
        REFERENCES session_revision(object_id, revision)
);

CREATE TABLE attempt_reference_v7 (
    attempt_id TEXT PRIMARY KEY,
    reference_contract_version INTEGER NOT NULL CHECK (
        reference_contract_version IN (1, 6, 7)
    ),
    session_id TEXT NOT NULL,
    session_revision INTEGER NOT NULL CHECK (session_revision >= 1),
    execution_intent_id TEXT,
    intent_sha256 TEXT NOT NULL CHECK (length(intent_sha256) = 64),
    allocation_id TEXT,
    allocation_sha256 TEXT CHECK (
        allocation_sha256 IS NULL OR length(allocation_sha256) = 64
    ),
    attempt_chain_id TEXT,
    attempt_sequence INTEGER CHECK (
        attempt_sequence IS NULL OR attempt_sequence >= 1
    ),
    previous_attempt_id TEXT,
    fence_id TEXT,
    fence_sha256 TEXT CHECK (
        fence_sha256 IS NULL OR length(fence_sha256) = 64
    ),
    intent_binding_digest TEXT CHECK (
        intent_binding_digest IS NULL OR length(intent_binding_digest) = 64
    ),
    journal_record_sha256 TEXT CHECK (
        journal_record_sha256 IS NULL OR length(journal_record_sha256) = 64
    ),
    staged_inventory_sha256 TEXT CHECK (
        staged_inventory_sha256 IS NULL OR length(staged_inventory_sha256) = 64
    ),
    prepared_receipt_sha256 TEXT CHECK (
        prepared_receipt_sha256 IS NULL OR length(prepared_receipt_sha256) = 64
    ),
    journal_schema_version INTEGER,
    attempt_owner TEXT NOT NULL CHECK (attempt_owner = 'workstation_control'),
    result_digest TEXT CHECK (
        result_digest IS NULL OR length(result_digest) = 64
    ),
    observed_lifecycle TEXT,
    settlement_state TEXT,
    unknown_effect INTEGER CHECK (
        unknown_effect IS NULL OR unknown_effect IN (0, 1)
    ),
    created_at TEXT,
    UNIQUE (execution_intent_id, attempt_sequence),
    UNIQUE (attempt_chain_id, attempt_sequence),
    UNIQUE (attempt_id, execution_intent_id, attempt_chain_id),
    UNIQUE (
        attempt_id, execution_intent_id, attempt_chain_id, attempt_sequence
    ),
    UNIQUE (
        attempt_id, execution_intent_id, attempt_chain_id,
        attempt_sequence, session_id, session_revision
    ),
    UNIQUE (attempt_id, session_id, session_revision),
    FOREIGN KEY (session_id, session_revision)
        REFERENCES session_revision(object_id, revision),
    FOREIGN KEY (execution_intent_id, session_id, session_revision)
        REFERENCES outbox_intent(intent_id, session_id, session_revision),
    FOREIGN KEY (
        allocation_id, execution_intent_id, session_id, session_revision
    ) REFERENCES session_attempt_allocation(
        allocation_id, intent_id, session_id, session_revision
    ),
    FOREIGN KEY (previous_attempt_id, execution_intent_id, attempt_chain_id)
        REFERENCES attempt_reference_v7(
            attempt_id, execution_intent_id, attempt_chain_id
        ),
    CHECK (
        (reference_contract_version = 1
            AND execution_intent_id IS NULL
            AND allocation_id IS NULL
            AND allocation_sha256 IS NULL
            AND attempt_chain_id IS NULL
            AND attempt_sequence IS NULL
            AND previous_attempt_id IS NULL
            AND fence_id IS NULL
            AND fence_sha256 IS NULL
            AND intent_binding_digest IS NULL
            AND journal_record_sha256 IS NULL
            AND staged_inventory_sha256 IS NULL
            AND prepared_receipt_sha256 IS NULL
            AND journal_schema_version IS NULL
            AND observed_lifecycle IS NOT NULL
            AND settlement_state IS NOT NULL
            AND unknown_effect IS NOT NULL)
        OR
        (reference_contract_version = 6
            AND execution_intent_id IS NOT NULL
            AND allocation_id IS NULL
            AND allocation_sha256 IS NULL
            AND attempt_chain_id IS NOT NULL
            AND attempt_sequence = 1
            AND previous_attempt_id IS NULL
            AND fence_id IS NOT NULL
            AND fence_sha256 IS NOT NULL
            AND intent_binding_digest IS NULL
            AND journal_record_sha256 IS NOT NULL
            AND staged_inventory_sha256 IS NULL
            AND prepared_receipt_sha256 IS NULL
            AND journal_schema_version IN (4, 5)
            AND (
                (result_digest IS NOT NULL
                    AND observed_lifecycle IS NOT NULL
                    AND settlement_state IS NOT NULL
                    AND unknown_effect IS NOT NULL)
                OR
                (result_digest IS NULL
                    AND observed_lifecycle IS NULL
                    AND settlement_state IS NULL
                    AND unknown_effect IS NULL)
            ))
        OR
        (reference_contract_version = 7
            AND execution_intent_id IS NOT NULL
            AND allocation_id IS NOT NULL
            AND allocation_sha256 IS NOT NULL
            AND attempt_chain_id IS NOT NULL
            AND attempt_sequence IS NOT NULL
            AND ((attempt_sequence = 1 AND previous_attempt_id IS NULL)
                 OR (attempt_sequence > 1 AND previous_attempt_id IS NOT NULL))
            AND fence_id IS NOT NULL
            AND fence_sha256 IS NOT NULL
            AND intent_binding_digest IS NOT NULL
            AND journal_record_sha256 IS NOT NULL
            AND staged_inventory_sha256 IS NOT NULL
            AND prepared_receipt_sha256 IS NOT NULL
            AND journal_schema_version = 5
            AND result_digest IS NULL
            AND observed_lifecycle IS NULL
            AND settlement_state IS NULL
            AND unknown_effect IS NULL
            AND created_at IS NOT NULL)
    )
);

-- Existing result projections that already have a prepared binding become
-- explicit contract-6 identities while retaining every legacy result column.
INSERT INTO attempt_reference_v7(
    attempt_id, reference_contract_version, session_id, session_revision,
    execution_intent_id, intent_sha256, allocation_id, allocation_sha256,
    attempt_chain_id, attempt_sequence, previous_attempt_id, fence_id,
    fence_sha256, intent_binding_digest, journal_record_sha256,
    staged_inventory_sha256, prepared_receipt_sha256,
    journal_schema_version, attempt_owner, result_digest,
    observed_lifecycle, settlement_state, unknown_effect, created_at
)
SELECT
    reference.attempt_id, 6, reference.session_id, reference.session_revision,
    binding.intent_id, reference.intent_digest, NULL, NULL,
    binding.attempt_chain_id, binding.attempt_sequence, NULL, binding.fence_id,
    binding.fence_digest, NULL, binding.prepared_record_digest,
    NULL, NULL, binding.workstation_journal_schema_version,
    'workstation_control', reference.result_digest,
    reference.observed_lifecycle, reference.settlement_state,
    reference.unknown_effect, binding.created_at
FROM attempt_reference AS reference
JOIN session_attempt_binding AS binding
  ON binding.attempt_id = reference.attempt_id;

-- Historical projections with no prepared binding retain their exact v1
-- result-only shape.  They are never retry- or dispatch-eligible.
INSERT INTO attempt_reference_v7(
    attempt_id, reference_contract_version, session_id, session_revision,
    execution_intent_id, intent_sha256, allocation_id, allocation_sha256,
    attempt_chain_id, attempt_sequence, previous_attempt_id, fence_id,
    fence_sha256, intent_binding_digest, journal_record_sha256,
    staged_inventory_sha256, prepared_receipt_sha256,
    journal_schema_version, attempt_owner, result_digest,
    observed_lifecycle, settlement_state, unknown_effect, created_at
)
SELECT
    reference.attempt_id, 1, reference.session_id, reference.session_revision,
    NULL, reference.intent_digest, NULL, NULL,
    NULL, NULL, NULL, NULL,
    NULL, NULL, NULL,
    NULL, NULL, NULL,
    'workstation_control', reference.result_digest,
    reference.observed_lifecycle, reference.settlement_state,
    reference.unknown_effect, NULL
FROM attempt_reference AS reference
WHERE NOT EXISTS (
    SELECT 1 FROM session_attempt_binding AS binding
    WHERE binding.attempt_id = reference.attempt_id
);

-- A v6 PREPARED binding may predate any result projection.  Its exact durable
-- identity is retained without inventing a result, observation, or receipt.
INSERT INTO attempt_reference_v7(
    attempt_id, reference_contract_version, session_id, session_revision,
    execution_intent_id, intent_sha256, allocation_id, allocation_sha256,
    attempt_chain_id, attempt_sequence, previous_attempt_id, fence_id,
    fence_sha256, intent_binding_digest, journal_record_sha256,
    staged_inventory_sha256, prepared_receipt_sha256,
    journal_schema_version, attempt_owner, result_digest,
    observed_lifecycle, settlement_state, unknown_effect, created_at
)
SELECT
    binding.attempt_id, 6, binding.session_id, binding.session_revision,
    binding.intent_id, binding.intent_digest, NULL, NULL,
    binding.attempt_chain_id, binding.attempt_sequence, NULL, binding.fence_id,
    binding.fence_digest, NULL, binding.prepared_record_digest,
    NULL, NULL, binding.workstation_journal_schema_version,
    'workstation_control', NULL, NULL, NULL, NULL,
    binding.created_at
FROM session_attempt_binding AS binding
WHERE NOT EXISTS (
    SELECT 1 FROM attempt_reference AS reference
    WHERE reference.attempt_id = binding.attempt_id
);

DROP TABLE attempt_reference;
ALTER TABLE attempt_reference_v7 RENAME TO attempt_reference;

CREATE TABLE inbox_receipt_v7 (
    receipt_id TEXT PRIMARY KEY,
    receipt_contract_version INTEGER NOT NULL CHECK (
        receipt_contract_version IN (1, 7)
    ),
    attempt_id TEXT NOT NULL,
    execution_intent_id TEXT,
    attempt_chain_id TEXT,
    attempt_sequence INTEGER CHECK (
        attempt_sequence IS NULL OR attempt_sequence >= 1
    ),
    observation_sequence INTEGER CHECK (
        observation_sequence IS NULL OR observation_sequence >= 1
    ),
    predecessor_receipt_id TEXT,
    predecessor_receipt_sha256 TEXT CHECK (
        predecessor_receipt_sha256 IS NULL OR
        length(predecessor_receipt_sha256) = 64
    ),
    envelope_digest TEXT NOT NULL CHECK (length(envelope_digest) = 64),
    envelope_json TEXT NOT NULL,
    termination_observation_sha256 TEXT CHECK (
        termination_observation_sha256 IS NULL OR
        length(termination_observation_sha256) = 64
    ),
    lifecycle TEXT NOT NULL CHECK (lifecycle IN ('inactive', 'received')),
    created_at TEXT NOT NULL,
    UNIQUE (attempt_id, envelope_digest),
    UNIQUE (attempt_id, observation_sequence),
    UNIQUE (predecessor_receipt_id),
    UNIQUE (receipt_id, attempt_id),
    FOREIGN KEY (attempt_id) REFERENCES attempt_reference(attempt_id),
    FOREIGN KEY (
        attempt_id, execution_intent_id, attempt_chain_id, attempt_sequence
    ) REFERENCES attempt_reference(
        attempt_id, execution_intent_id, attempt_chain_id, attempt_sequence
    ),
    FOREIGN KEY (predecessor_receipt_id, attempt_id)
        REFERENCES inbox_receipt_v7(receipt_id, attempt_id),
    CHECK (
        (receipt_contract_version = 1
            AND execution_intent_id IS NULL
            AND attempt_chain_id IS NULL
            AND attempt_sequence IS NULL
            AND observation_sequence IS NULL
            AND predecessor_receipt_id IS NULL
            AND predecessor_receipt_sha256 IS NULL
            AND termination_observation_sha256 IS NULL)
        OR
        (receipt_contract_version = 7
            AND execution_intent_id IS NOT NULL
            AND attempt_chain_id IS NOT NULL
            AND attempt_sequence IS NOT NULL
            AND observation_sequence IS NOT NULL
            AND ((observation_sequence = 1
                    AND predecessor_receipt_id IS NULL
                    AND predecessor_receipt_sha256 IS NULL)
                 OR (observation_sequence > 1
                    AND predecessor_receipt_id IS NOT NULL
                    AND predecessor_receipt_sha256 IS NOT NULL)))
    )
);

INSERT INTO inbox_receipt_v7(
    receipt_id, receipt_contract_version, attempt_id, execution_intent_id,
    attempt_chain_id, attempt_sequence, observation_sequence,
    predecessor_receipt_id, predecessor_receipt_sha256, envelope_digest,
    envelope_json, termination_observation_sha256, lifecycle, created_at
)
SELECT
    receipt_id, 1, attempt_id, NULL, NULL, NULL, NULL,
    NULL, NULL, envelope_digest, envelope_json, NULL, lifecycle, created_at
FROM inbox_receipt;

DROP TABLE inbox_receipt;
ALTER TABLE inbox_receipt_v7 RENAME TO inbox_receipt;

CREATE INDEX inbox_receipt_attempt
ON inbox_receipt(attempt_id, observation_sequence, receipt_id);

CREATE TABLE session_attempt_disposition_transition (
    disposition_id TEXT PRIMARY KEY,
    disposition_contract_version INTEGER NOT NULL CHECK (
        disposition_contract_version = 1
    ),
    session_id TEXT NOT NULL,
    session_revision INTEGER NOT NULL CHECK (session_revision >= 1),
    attempt_id TEXT NOT NULL,
    transition_sequence INTEGER NOT NULL CHECK (transition_sequence >= 1),
    predecessor_disposition_id TEXT,
    predecessor_disposition_sha256 TEXT CHECK (
        predecessor_disposition_sha256 IS NULL OR
        length(predecessor_disposition_sha256) = 64
    ),
    result_receipt_id TEXT NOT NULL UNIQUE,
    result_digest TEXT NOT NULL CHECK (length(result_digest) = 64),
    termination_observation_sha256 TEXT NOT NULL CHECK (
        length(termination_observation_sha256) = 64
    ),
    action TEXT NOT NULL CHECK (action IN (
        'semantic_pending',
        'retry_authorized',
        'terminal_operational',
        'blocked_unknown'
    )),
    normalized_reason TEXT NOT NULL CHECK (normalized_reason IN (
        'provider_succeeded',
        'provider_failed',
        'provider_cancelled',
        'cancelled_before_effect',
        'provider_rejected_before_effect',
        'fenced_before_effect',
        'fenced_after_effect',
        'unknown_effect'
    )),
    disposition_sha256 TEXT NOT NULL UNIQUE CHECK (length(disposition_sha256) = 64),
    created_at TEXT NOT NULL,
    row_digest TEXT NOT NULL CHECK (length(row_digest) = 64),
    UNIQUE (session_id, session_revision, transition_sequence),
    UNIQUE (predecessor_disposition_id),
    UNIQUE (disposition_id, attempt_id, action),
    UNIQUE (disposition_id, session_id, session_revision),
    UNIQUE (disposition_id, attempt_id, session_id, session_revision),
    FOREIGN KEY (session_id, session_revision)
        REFERENCES session_revision(object_id, revision),
    FOREIGN KEY (attempt_id, session_id, session_revision)
        REFERENCES attempt_reference(attempt_id, session_id, session_revision),
    FOREIGN KEY (result_receipt_id, attempt_id)
        REFERENCES inbox_receipt(receipt_id, attempt_id),
    FOREIGN KEY (
        predecessor_disposition_id, session_id, session_revision
    ) REFERENCES session_attempt_disposition_transition(
        disposition_id, session_id, session_revision
    ),
    CHECK (
        (transition_sequence = 1
            AND predecessor_disposition_id IS NULL
            AND predecessor_disposition_sha256 IS NULL)
        OR
        (transition_sequence > 1
            AND predecessor_disposition_id IS NOT NULL
            AND predecessor_disposition_sha256 IS NOT NULL)
    ),
    CHECK (
        (action = 'blocked_unknown' AND normalized_reason = 'unknown_effect')
        OR
        (action = 'retry_authorized' AND normalized_reason = 'provider_failed')
        OR
        (action = 'semantic_pending' AND normalized_reason = 'provider_succeeded')
        OR
        (action = 'terminal_operational' AND normalized_reason IN (
            'provider_failed',
            'provider_cancelled',
            'cancelled_before_effect',
            'provider_rejected_before_effect',
            'fenced_before_effect',
            'fenced_after_effect'
        ))
    )
);

CREATE TABLE session_attempt_binding_v7 (
    binding_id TEXT PRIMARY KEY,
    binding_contract_version INTEGER NOT NULL CHECK (
        binding_contract_version IN (6, 7)
    ),
    intent_id TEXT NOT NULL,
    intent_digest TEXT NOT NULL CHECK (length(intent_digest) = 64),
    allocation_id TEXT,
    project_id TEXT NOT NULL,
    mission_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    session_revision INTEGER NOT NULL CHECK (session_revision >= 1),
    attempt_id TEXT NOT NULL UNIQUE,
    attempt_chain_id TEXT NOT NULL,
    attempt_sequence INTEGER NOT NULL CHECK (attempt_sequence >= 1),
    previous_attempt_id TEXT,
    retry_authorization_disposition_id TEXT,
    retry_authorization_action TEXT CHECK (
        retry_authorization_action IS NULL OR
        retry_authorization_action = 'retry_authorized'
    ),
    workstation_journal_schema_version INTEGER NOT NULL CHECK (
        workstation_journal_schema_version IN (4, 5)
    ),
    workstation_attempt_state TEXT NOT NULL CHECK (
        workstation_attempt_state = 'prepared'
    ),
    fence_id TEXT NOT NULL,
    fence_digest TEXT NOT NULL CHECK (length(fence_digest) = 64),
    intent_binding_digest TEXT CHECK (
        intent_binding_digest IS NULL OR length(intent_binding_digest) = 64
    ),
    journal_record_sha256 TEXT NOT NULL CHECK (length(journal_record_sha256) = 64),
    staged_inventory_sha256 TEXT CHECK (
        staged_inventory_sha256 IS NULL OR length(staged_inventory_sha256) = 64
    ),
    prepared_receipt_sha256 TEXT CHECK (
        prepared_receipt_sha256 IS NULL OR length(prepared_receipt_sha256) = 64
    ),
    coordination_id TEXT NOT NULL,
    coordination_digest TEXT NOT NULL CHECK (length(coordination_digest) = 64),
    authorization_id TEXT NOT NULL,
    authorization_digest TEXT NOT NULL CHECK (length(authorization_digest) = 64),
    created_at TEXT NOT NULL,
    UNIQUE (intent_id, attempt_sequence),
    UNIQUE (attempt_chain_id, attempt_sequence),
    UNIQUE (previous_attempt_id),
    UNIQUE (retry_authorization_disposition_id),
    UNIQUE (binding_id, attempt_id),
    FOREIGN KEY (
        allocation_id, intent_id, session_id, session_revision
    ) REFERENCES session_attempt_allocation(
        allocation_id, intent_id, session_id, session_revision
    ),
    FOREIGN KEY (
        attempt_id, intent_id, attempt_chain_id, attempt_sequence,
        session_id, session_revision
    ) REFERENCES attempt_reference(
        attempt_id, execution_intent_id, attempt_chain_id, attempt_sequence,
        session_id, session_revision
    ),
    FOREIGN KEY (previous_attempt_id) REFERENCES attempt_reference(attempt_id),
    FOREIGN KEY (
        retry_authorization_disposition_id,
        previous_attempt_id,
        retry_authorization_action
    ) REFERENCES session_attempt_disposition_transition(
        disposition_id, attempt_id, action
    ),
    CHECK (
        (binding_contract_version = 6
            AND allocation_id IS NULL
            AND attempt_sequence = 1
            AND previous_attempt_id IS NULL
            AND retry_authorization_disposition_id IS NULL
            AND retry_authorization_action IS NULL
            AND intent_binding_digest IS NULL
            AND staged_inventory_sha256 IS NULL
            AND prepared_receipt_sha256 IS NULL)
        OR
        (binding_contract_version = 7
            AND allocation_id IS NOT NULL
            AND workstation_journal_schema_version = 5
            AND intent_binding_digest IS NOT NULL
            AND staged_inventory_sha256 IS NOT NULL
            AND prepared_receipt_sha256 IS NOT NULL
            AND ((attempt_sequence = 1
                    AND previous_attempt_id IS NULL
                    AND retry_authorization_disposition_id IS NULL
                    AND retry_authorization_action IS NULL)
                 OR (attempt_sequence > 1
                    AND previous_attempt_id IS NOT NULL
                    AND retry_authorization_disposition_id IS NOT NULL
                    AND retry_authorization_action = 'retry_authorized')))
    )
);

INSERT INTO session_attempt_binding_v7(
    binding_id, binding_contract_version, intent_id, intent_digest,
    allocation_id, project_id, mission_id, session_id, session_revision,
    attempt_id, attempt_chain_id, attempt_sequence, previous_attempt_id,
    retry_authorization_disposition_id, retry_authorization_action,
    workstation_journal_schema_version, workstation_attempt_state,
    fence_id, fence_digest, intent_binding_digest, journal_record_sha256,
    staged_inventory_sha256, prepared_receipt_sha256, coordination_id,
    coordination_digest, authorization_id, authorization_digest, created_at
)
SELECT
    binding_id, 6, intent_id, intent_digest,
    NULL, project_id, mission_id, session_id, session_revision,
    attempt_id, attempt_chain_id, attempt_sequence, NULL,
    NULL, NULL,
    workstation_journal_schema_version, workstation_attempt_state,
    fence_id, fence_digest, NULL, prepared_record_digest,
    NULL, NULL, coordination_id,
    coordination_digest, authorization_id, authorization_digest, created_at
FROM session_attempt_binding;

DROP TABLE session_attempt_binding;
ALTER TABLE session_attempt_binding_v7 RENAME TO session_attempt_binding;

CREATE TABLE prepared_attempt_receipt (
    attempt_id TEXT PRIMARY KEY,
    binding_id TEXT NOT NULL UNIQUE,
    receipt_contract_version INTEGER NOT NULL CHECK (
        receipt_contract_version = 1
    ),
    receipt_json TEXT NOT NULL,
    receipt_sha256 TEXT NOT NULL UNIQUE CHECK (length(receipt_sha256) = 64),
    created_at TEXT NOT NULL,
    row_digest TEXT NOT NULL CHECK (length(row_digest) = 64),
    FOREIGN KEY (binding_id, attempt_id)
        REFERENCES session_attempt_binding(binding_id, attempt_id)
);

CREATE TABLE attempt_evidence_binding_v7 (
    binding_id TEXT PRIMARY KEY,
    binding_contract_version INTEGER NOT NULL CHECK (
        binding_contract_version IN (1, 7)
    ),
    attempt_id TEXT NOT NULL,
    result_receipt_id TEXT NOT NULL UNIQUE,
    result_digest TEXT NOT NULL CHECK (length(result_digest) = 64),
    evidence_id TEXT NOT NULL,
    evidence_revision INTEGER NOT NULL CHECK (evidence_revision >= 1),
    evidence_payload_digest TEXT NOT NULL CHECK (length(evidence_payload_digest) = 64),
    closure_id TEXT NOT NULL,
    closure_digest TEXT NOT NULL CHECK (length(closure_digest) = 64),
    created_at TEXT NOT NULL,
    UNIQUE (attempt_id, result_digest),
    UNIQUE (binding_id, attempt_id),
    FOREIGN KEY (attempt_id) REFERENCES attempt_reference(attempt_id),
    FOREIGN KEY (result_receipt_id, attempt_id)
        REFERENCES inbox_receipt(receipt_id, attempt_id),
    FOREIGN KEY (evidence_id, evidence_revision)
        REFERENCES evidence_item_revision(evidence_id, revision),
    FOREIGN KEY (closure_id) REFERENCES closure_manifest(closure_id)
);

INSERT INTO attempt_evidence_binding_v7(
    binding_id, binding_contract_version, attempt_id, result_receipt_id,
    result_digest, evidence_id, evidence_revision, evidence_payload_digest,
    closure_id, closure_digest, created_at
)
SELECT
    binding_id, 1, attempt_id, result_receipt_id,
    result_digest, evidence_id, evidence_revision, evidence_payload_digest,
    closure_id, closure_digest, created_at
FROM attempt_evidence_binding;

DROP TABLE attempt_evidence_binding;
ALTER TABLE attempt_evidence_binding_v7 RENAME TO attempt_evidence_binding;

CREATE TABLE session_settlement_v7 (
    settlement_id TEXT PRIMARY KEY,
    settlement_contract_version INTEGER NOT NULL CHECK (
        settlement_contract_version IN (1, 7)
    ),
    session_id TEXT NOT NULL,
    session_revision INTEGER NOT NULL CHECK (session_revision >= 1),
    attempt_references_json TEXT NOT NULL,
    attempt_chain_sha256 TEXT CHECK (
        attempt_chain_sha256 IS NULL OR length(attempt_chain_sha256) = 64
    ),
    final_attempt_id TEXT,
    allocation_id TEXT,
    disposition_head_id TEXT,
    disposition_head_sha256 TEXT CHECK (
        disposition_head_sha256 IS NULL OR length(disposition_head_sha256) = 64
    ),
    charge_basis_json TEXT NOT NULL,
    charge_basis_sha256 TEXT CHECK (
        charge_basis_sha256 IS NULL OR length(charge_basis_sha256) = 64
    ),
    release_conversion_json TEXT NOT NULL,
    release_conversion_sha256 TEXT CHECK (
        release_conversion_sha256 IS NULL OR length(release_conversion_sha256) = 64
    ),
    evidence_binding_id TEXT,
    command_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    UNIQUE (session_id, session_revision),
    FOREIGN KEY (session_id, session_revision)
        REFERENCES session_revision(object_id, revision),
    FOREIGN KEY (final_attempt_id, session_id, session_revision)
        REFERENCES attempt_reference(attempt_id, session_id, session_revision),
    FOREIGN KEY (allocation_id, session_id, session_revision)
        REFERENCES session_attempt_allocation(
            allocation_id, session_id, session_revision
        ),
    FOREIGN KEY (
        disposition_head_id, final_attempt_id, session_id, session_revision
    ) REFERENCES session_attempt_disposition_transition(
        disposition_id, attempt_id, session_id, session_revision
    ),
    FOREIGN KEY (evidence_binding_id, final_attempt_id)
        REFERENCES attempt_evidence_binding(binding_id, attempt_id),
    FOREIGN KEY (evidence_binding_id)
        REFERENCES attempt_evidence_binding(binding_id),
    CHECK (
        (settlement_contract_version = 1
            AND attempt_chain_sha256 IS NULL
            AND final_attempt_id IS NULL
            AND allocation_id IS NULL
            AND disposition_head_id IS NULL
            AND disposition_head_sha256 IS NULL
            AND charge_basis_sha256 IS NULL
            AND release_conversion_sha256 IS NULL)
        OR
        (settlement_contract_version = 7
            AND attempt_chain_sha256 IS NOT NULL
            AND final_attempt_id IS NOT NULL
            AND allocation_id IS NOT NULL
            AND disposition_head_id IS NOT NULL
            AND disposition_head_sha256 IS NOT NULL
            AND charge_basis_sha256 IS NOT NULL
            AND release_conversion_sha256 IS NOT NULL)
    )
);

INSERT INTO session_settlement_v7(
    settlement_id, settlement_contract_version, session_id, session_revision,
    attempt_references_json, attempt_chain_sha256, final_attempt_id,
    allocation_id, disposition_head_id, disposition_head_sha256,
    charge_basis_json, charge_basis_sha256, release_conversion_json,
    release_conversion_sha256, evidence_binding_id, command_id, created_at
)
SELECT
    settlement_id, 1, session_id, session_revision,
    attempt_references_json, NULL, NULL,
    NULL, NULL, NULL,
    charge_basis_json, NULL, release_conversion_json,
    NULL, evidence_binding_id, command_id, created_at
FROM session_settlement;

DROP TABLE session_settlement;
ALTER TABLE session_settlement_v7 RENAME TO session_settlement;

CREATE TABLE workspace_root_contract_transition (
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
    source_schema_version INTEGER NOT NULL CHECK (source_schema_version = 6),
    target_schema_version INTEGER NOT NULL CHECK (target_schema_version = 7),
    source_root_digest_version INTEGER NOT NULL CHECK (
        source_root_digest_version = 3
    ),
    target_root_digest_version INTEGER NOT NULL CHECK (
        target_root_digest_version = 4
    ),
    target_migration_set_digest TEXT NOT NULL CHECK (
        length(target_migration_set_digest) = 64
    ),
    target_schema_object_digest TEXT NOT NULL CHECK (
        length(target_schema_object_digest) = 64
    ),
    canonical_effect TEXT NOT NULL CHECK (canonical_effect = 'none'),
    created_at TEXT NOT NULL,
    row_digest TEXT NOT NULL CHECK (length(row_digest) = 64),
    FOREIGN KEY (writer_epoch) REFERENCES writer_epoch(epoch),
    FOREIGN KEY (migration_execution_id, migration_attempt_id)
        REFERENCES migration_execution(execution_id, attempt_id),
    FOREIGN KEY (source_project_commit) REFERENCES project_commit(commit_no)
);
