-- Admit only the current Workstation PREPARED Attempt contract. Historical
-- contract-6/7 and journal-4/5 rows remain byte-exact and non-launchable.

CREATE TABLE attempt_reference_v8 (
    attempt_id TEXT PRIMARY KEY,
    reference_contract_version INTEGER NOT NULL CHECK (
        reference_contract_version IN (1, 6, 7, 8)
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
        REFERENCES attempt_reference_v8(
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
            AND ((result_digest IS NOT NULL
                    AND observed_lifecycle IS NOT NULL
                    AND settlement_state IS NOT NULL
                    AND unknown_effect IS NOT NULL)
                 OR (result_digest IS NULL
                    AND observed_lifecycle IS NULL
                    AND settlement_state IS NULL
                    AND unknown_effect IS NULL)))
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
        OR
        (reference_contract_version = 8
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
            AND journal_schema_version = 6
            AND result_digest IS NULL
            AND observed_lifecycle IS NULL
            AND settlement_state IS NULL
            AND unknown_effect IS NULL
            AND created_at IS NOT NULL)
    )
);

INSERT INTO attempt_reference_v8 SELECT * FROM attempt_reference;
DROP TABLE attempt_reference;
ALTER TABLE attempt_reference_v8 RENAME TO attempt_reference;

CREATE TABLE session_attempt_binding_v8 (
    binding_id TEXT PRIMARY KEY,
    binding_contract_version INTEGER NOT NULL CHECK (
        binding_contract_version IN (6, 7, 8)
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
        workstation_journal_schema_version IN (4, 5, 6)
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
        OR
        (binding_contract_version = 8
            AND allocation_id IS NOT NULL
            AND workstation_journal_schema_version = 6
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

INSERT INTO session_attempt_binding_v8 SELECT * FROM session_attempt_binding;
DROP TABLE session_attempt_binding;
ALTER TABLE session_attempt_binding_v8 RENAME TO session_attempt_binding;

CREATE TABLE prepared_attempt_receipt_v8 (
    attempt_id TEXT PRIMARY KEY,
    binding_id TEXT NOT NULL UNIQUE,
    receipt_contract_version INTEGER NOT NULL CHECK (
        receipt_contract_version IN (1, 2)
    ),
    receipt_json TEXT NOT NULL,
    receipt_sha256 TEXT NOT NULL UNIQUE CHECK (length(receipt_sha256) = 64),
    created_at TEXT NOT NULL,
    row_digest TEXT NOT NULL CHECK (length(row_digest) = 64),
    FOREIGN KEY (binding_id, attempt_id)
        REFERENCES session_attempt_binding(binding_id, attempt_id),
    CHECK (
        receipt_contract_version = 1
        OR json_extract(receipt_json, '$.schema')
            = 'wc.research_attempt_prepared_receipt.v2'
    )
);

INSERT INTO prepared_attempt_receipt_v8 SELECT * FROM prepared_attempt_receipt;
DROP TABLE prepared_attempt_receipt;
ALTER TABLE prepared_attempt_receipt_v8 RENAME TO prepared_attempt_receipt;
