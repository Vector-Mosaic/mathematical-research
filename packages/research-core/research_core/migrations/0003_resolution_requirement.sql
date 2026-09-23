CREATE TABLE evidence_relation_v3 (
    relation_id TEXT PRIMARY KEY,
    source_evidence_id TEXT NOT NULL,
    source_revision INTEGER NOT NULL,
    target_evidence_id TEXT NOT NULL,
    target_revision INTEGER NOT NULL,
    relation_kind TEXT NOT NULL CHECK (relation_kind IN (
        'successor', 'correction', 'reproduction', 'review', 'duplicate',
        'dispute', 'consumer_use', 'resolution_requirement'
    )),
    scope_json TEXT NOT NULL,
    FOREIGN KEY (source_evidence_id, source_revision)
        REFERENCES evidence_item_revision(evidence_id, revision),
    FOREIGN KEY (target_evidence_id, target_revision)
        REFERENCES evidence_item_revision(evidence_id, revision)
);

INSERT INTO evidence_relation_v3 (
    relation_id,
    source_evidence_id,
    source_revision,
    target_evidence_id,
    target_revision,
    relation_kind,
    scope_json
)
SELECT
    relation_id,
    source_evidence_id,
    source_revision,
    target_evidence_id,
    target_revision,
    relation_kind,
    scope_json
FROM evidence_relation;

DROP TABLE evidence_relation;

ALTER TABLE evidence_relation_v3 RENAME TO evidence_relation;
