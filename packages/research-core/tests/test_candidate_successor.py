from __future__ import annotations

import copy
import hashlib
import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from jsonschema import Draft202012Validator


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = Path(__file__).resolve().parents[3]
if str(PACKAGE_ROOT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_ROOT))

from research_core.candidate_revision import (  # noqa: E402
    CANDIDATE_REVISION_SCHEMA_REPO_PATH,
    CANDIDATE_REVISION_V3_SCHEMA_REPO_PATH,
    CANDIDATE_REVISION_V4_SCHEMA_REPO_PATH,
    CandidateRevisionV2,
    CandidateRevisionV3,
    CandidateRevisionV4,
    PersistedCandidateRevisionV2,
    PersistedCandidateRevisionV3,
    PersistedCandidateRevisionV4,
    candidate_a1_requirement,
    candidate_complete_target_claim,
    candidate_requires_a1,
    candidate_schema_digest,
    candidate_schema_digest_v3,
    candidate_schema_digest_v4,
    commit_candidate_revision,
    _prepare_candidate_revision_v2_legacy_document,
    prepare_candidate_revision,
    prepare_candidate_revision_v3,
    read_candidate_revision,
    validate_candidate_revision_v2,
    validate_candidate_revision_v3,
    validate_candidate_revision_v4,
)
from research_core.json_support import canonical_json_bytes  # noqa: E402
from research_core.mission_executive import (  # noqa: E402
    DirectExecutiveEpochAuthority,
    OwnerRevisionRef,
    prepare_direct_executive_epoch_authorized_event,
    prepare_direct_executive_epoch_bound_event,
    reissue_direct_executive_epoch_authority,
)
from research_core.research_model import deep_thaw  # noqa: E402


def _active_epoch_readbacks(
    *, epoch_id: str = "epoch.candidate.1"
) -> tuple[dict[str, Any], dict[str, Any]]:
    authorized = prepare_direct_executive_epoch_authorized_event(
        executive_epoch_id=epoch_id,
        mission_root=OwnerRevisionRef("mission", "mission.1", 1, "1" * 64),
        predecessor_checkpoint=None,
    )
    bound = prepare_direct_executive_epoch_bound_event(
        executive_epoch_id=epoch_id,
        mission_id="mission.1",
        goal_thread_id=f"thread:{epoch_id}",
        workspace_root="C:/work/rh",
    )

    def readback(event, ordinal: int, predecessor) -> dict[str, Any]:
        return {
            "executive_epoch_id": event.executive_epoch_id,
            "event_ordinal": ordinal,
            "mission_id": event.mission_id,
            "project_commit_no": ordinal,
            "event_kind": event.event_kind,
            "event": deep_thaw(event.document),
            "event_digest": event.digest_sha256,
            "predecessor_event_ordinal": None if predecessor is None else 1,
            "predecessor_event_digest": (
                None if predecessor is None else predecessor.digest_sha256
            ),
            "created_actor": "coordinating-codex",
            "created_at": f"2026-08-23T12:00:0{ordinal}Z",
            "row_digest": str(ordinal) * 64,
        }

    return (
        readback(authorized, 1, None),
        readback(bound, 2, authorized),
    )


def _authority(*, epoch_id: str = "epoch.candidate.1") -> DirectExecutiveEpochAuthority:
    return reissue_direct_executive_epoch_authority(
        event_readbacks=_active_epoch_readbacks(epoch_id=epoch_id),
        project_id="project.rh",
        root_identity="root.rh",
        canonical_authority_digest="c" * 64,
    )


class _CandidateStoreFake:
    def __init__(self) -> None:
        self.commit_calls: list[dict[str, Any]] = []
        self.rows: dict[str, list[dict[str, Any]]] = {}
        self.active_event_readbacks = _active_epoch_readbacks()

    def read_active_executive_epoch(
        self,
        mission_id: str,
    ) -> tuple[dict[str, Any], dict[str, Any]] | None:
        if mission_id != "mission.1":
            return None
        return copy.deepcopy(self.active_event_readbacks)

    def read_metadata(self) -> dict[str, Any]:
        return {
            "project_id": "project.rh",
            "root_identity": "root.rh",
            "canonical_authority_digest": "c" * 64,
        }

    def commit_candidate_revision(self, **kwargs: Any) -> Any:
        self.commit_calls.append(kwargs)
        return SimpleNamespace(replayed=False, project_commit=1)

    def read_candidate_revision(
        self,
        *,
        candidate_id: str,
        mission_id: str,
        revision: int | None = None,
    ) -> dict[str, Any]:
        if mission_id != "mission.1":
            raise KeyError(mission_id)
        rows = self.rows[candidate_id]
        row = rows[-1] if revision is None else rows[revision - 1]
        return copy.deepcopy(row)


def _persisted(
    record: CandidateRevisionV4,
    *,
    revision: int = 1,
    current: bool = True,
) -> PersistedCandidateRevisionV4:
    return PersistedCandidateRevisionV4(
        record=record,
        revision=revision,
        payload_digest=record.digest_sha256,
        predecessor_revision=None if revision == 1 else revision - 1,
        created_actor="coordinating-codex",
        created_at="2026-08-23T12:00:00Z",
        is_current_head=current,
    )


def _persisted_v3(
    record: CandidateRevisionV3,
    *,
    revision: int,
    current: bool = True,
) -> PersistedCandidateRevisionV3:
    return PersistedCandidateRevisionV3(
        record=record,
        revision=revision,
        payload_digest=record.digest_sha256,
        predecessor_revision=None if revision == 1 else revision - 1,
        created_actor="coordinating-codex",
        created_at="2026-08-23T12:00:00Z",
        is_current_head=current,
    )


def _base_candidate(
    authority: DirectExecutiveEpochAuthority,
    **overrides: Any,
) -> CandidateRevisionV4:
    values: dict[str, Any] = {
        "authority": authority,
        "candidate_id": "candidate.theta-transfer.1",
        "proposal_kind": "lemma",
        "exact_statement": (
            "The normalized transfer operator preserves the stated test-function core."
        ),
        "standing": {
            "status": "open",
            "basis": "The transfer identity remains subject to its exact obstruction test.",
        },
        "objects": ("transfer operator", "test-function core"),
        "hypotheses": ("the core is invariant", "the normalization is fixed"),
        "domain": "the stated test-function core",
        "quantifiers": ("for every normalized core element",),
        "normalization": ("unit core norm",),
        "obligations": ("prove closure on the exact domain",),
        "falsifiers": ("one normalized core element leaving the core",),
        "non_inferences": ("does not establish the Riemann Hypothesis",),
    }
    values.update(overrides)
    return prepare_candidate_revision(**values, repo_root=REPO_ROOT)


def _historical_candidate_v2(
    authority: DirectExecutiveEpochAuthority,
    **overrides: Any,
) -> CandidateRevisionV2:
    """Build immutable v2 material only as an explicit historical test fixture."""

    values: dict[str, Any] = {
        "authority": authority,
        "candidate_id": "candidate.theta-transfer.1",
        "proposal_kind": "lemma",
        "exact_statement": (
            "The normalized transfer operator preserves the stated test-function core."
        ),
        "standing": {
            "status": "open",
            "basis": "Historical Candidate-v2 fixture.",
        },
        "objects": ("transfer operator", "test-function core"),
        "hypotheses": ("the core is invariant", "the normalization is fixed"),
        "domain": "the stated test-function core",
        "quantifiers": ("for every normalized core element",),
        "normalization": ("unit core norm",),
        "obligations": ("prove closure on the exact domain",),
        "falsifiers": ("one normalized core element leaving the core",),
        "non_inferences": ("does not establish the Riemann Hypothesis",),
    }
    values.update(overrides)
    return _prepare_candidate_revision_v2_legacy_document(
        **values, repo_root=REPO_ROOT
    )


class CandidateSuccessorContractTests(unittest.TestCase):
    def test_v2_is_historical_read_only_at_the_current_core_boundary(self) -> None:
        authority = _authority()
        historical = _historical_candidate_v2(authority)
        store = _CandidateStoreFake()
        store.rows[str(historical.document["candidate_id"])] = [
            {
                "payload": deep_thaw(historical.document),
                "revision": 1,
                "payload_digest": historical.digest_sha256,
                "predecessor_revision": None,
                "created_actor": "historical-fixture",
                "created_at": "2026-08-23T12:00:00Z",
            }
        ]
        readback = read_candidate_revision(
            store,
            mission_id="mission.1",
            candidate_id=str(historical.document["candidate_id"]),
        )
        self.assertIsInstance(readback, PersistedCandidateRevisionV2)
        with self.assertRaisesRegex(TypeError, "Candidate-v4"):
            commit_candidate_revision(
                store,
                authority=authority,
                record=historical,
                lease="lease.1",
                actor="coordinating-codex",
            )
        self.assertEqual(store.commit_calls, [])

    def test_v3_uses_structured_complete_target_claim_without_wording_regex(
        self,
    ) -> None:
        schema_path = REPO_ROOT / CANDIDATE_REVISION_V3_SCHEMA_REPO_PATH
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        self.assertEqual(
            candidate_schema_digest_v3(REPO_ROOT),
            hashlib.sha256(schema_path.read_bytes()).hexdigest(),
        )

        authority = _authority()
        wording_only = prepare_candidate_revision_v3(
            authority=authority,
            candidate_id="candidate.rh.wording-only.1",
            proposal_kind="proof_architecture",
            exact_statement="This purported argument proves the Riemann Hypothesis.",
            standing={"status": "open", "basis": "requires exact review"},
            mechanism="A purported unconditional argument proves the exact statement.",
            scope_and_reach="complete unconditional proof of RH",
            repo_root=REPO_ROOT,
        )
        self.assertFalse(candidate_requires_a1(wording_only.document))
        self.assertIsNone(candidate_a1_requirement(wording_only.document))

        structured = prepare_candidate_revision_v3(
            authority=authority,
            candidate_id="candidate.rh.structured.1",
            proposal_kind="proof_architecture",
            exact_statement="The attached material reaches the stated target.",
            standing={"status": "open", "basis": "requires exact review"},
            mechanism="One claimed proof architecture reaches the stated target.",
            complete_target_claim={
                "target": "riemann_hypothesis",
                "disposition": "disproof",
            },
            repo_root=REPO_ROOT,
        )
        self.assertTrue(validate_candidate_revision_v3(structured.document).ok)
        self.assertTrue(candidate_requires_a1(structured.document))
        self.assertEqual(
            deep_thaw(candidate_complete_target_claim(structured.document)),
            {"target": "riemann_hypothesis", "disposition": "disproof"},
        )
        self.assertIsNotNone(candidate_a1_requirement(structured.document))

        store = _CandidateStoreFake()
        store.rows[str(structured.document["candidate_id"])] = [
            {
                "payload": deep_thaw(structured.document),
                "revision": 1,
                "payload_digest": structured.digest_sha256,
                "predecessor_revision": None,
                "created_actor": "coordinating-codex",
                "created_at": "2026-08-23T12:00:00Z",
            }
        ]
        self.assertIsInstance(
            read_candidate_revision(
                store,
                mission_id="mission.1",
                candidate_id=str(structured.document["candidate_id"]),
            ),
            PersistedCandidateRevisionV3,
        )
        with self.assertRaisesRegex(TypeError, "Candidate-v4"):
            commit_candidate_revision(
                store,
                authority=authority,
                record=structured,
                lease="lease.1",
                actor="coordinating-codex",
            )
        self.assertEqual(store.commit_calls, [])

        invalid = deep_thaw(structured.document)
        invalid["complete_target_claim"]["disposition"] = "unknown"
        self.assertFalse(validate_candidate_revision_v3(invalid).ok)

    def test_same_identity_evolves_v2_to_v3_and_may_change_or_remove_claim(
        self,
    ) -> None:
        authority = _authority()
        v2 = _historical_candidate_v2(authority)
        v2_persisted = PersistedCandidateRevisionV2(
            record=v2,
            revision=1,
            payload_digest=v2.digest_sha256,
            predecessor_revision=None,
            created_actor="historical-fixture",
            created_at="2026-08-23T12:00:00Z",
            is_current_head=True,
        )
        v3 = prepare_candidate_revision_v3(
            authority=authority,
            candidate_id=str(v2.document["candidate_id"]),
            proposal_kind=str(v2.document["proposal_kind"]),
            exact_statement=str(v2.document["exact_statement"]),
            standing={"status": "open", "basis": "complete reach is now claimed"},
            predecessor=v2_persisted,
            complete_target_claim={
                "target": "riemann_hypothesis",
                "disposition": "proof",
            },
            hypotheses=tuple(v2.document["hypotheses"]),
            domain=str(v2.document["domain"]),
            normalization=tuple(v2.document["normalization"]),
            repo_root=REPO_ROOT,
        )
        v3_persisted = _persisted_v3(v3, revision=2)
        changed = prepare_candidate_revision_v3(
            authority=authority,
            candidate_id=str(v3.document["candidate_id"]),
            proposal_kind=str(v3.document["proposal_kind"]),
            exact_statement=str(v3.document["exact_statement"]),
            standing={"status": "open", "basis": "disposition corrected"},
            predecessor=v3_persisted,
            complete_target_claim={
                "target": "riemann_hypothesis",
                "disposition": "disproof",
            },
            hypotheses=tuple(v3.document["hypotheses"]),
            domain=str(v3.document["domain"]),
            normalization=tuple(v3.document["normalization"]),
            repo_root=REPO_ROOT,
        )
        removed = prepare_candidate_revision_v3(
            authority=authority,
            candidate_id=str(changed.document["candidate_id"]),
            proposal_kind=str(changed.document["proposal_kind"]),
            exact_statement=str(changed.document["exact_statement"]),
            standing={"status": "withdrawn", "basis": "complete reach withdrawn"},
            predecessor=_persisted_v3(changed, revision=3),
            hypotheses=tuple(changed.document["hypotheses"]),
            domain=str(changed.document["domain"]),
            normalization=tuple(changed.document["normalization"]),
            repo_root=REPO_ROOT,
        )
        self.assertIsNone(candidate_complete_target_claim(v2.document))
        self.assertEqual(
            deep_thaw(candidate_complete_target_claim(changed.document)),
            {"target": "riemann_hypothesis", "disposition": "disproof"},
        )
        self.assertFalse(candidate_requires_a1(removed.document))

    def test_public_schema_validates_and_sparse_document_has_no_runtime_filler(
        self,
    ) -> None:
        schema_path = REPO_ROOT / CANDIDATE_REVISION_SCHEMA_REPO_PATH
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(schema)
        self.assertEqual(
            candidate_schema_digest(REPO_ROOT),
            hashlib.sha256(schema_path.read_bytes()).hexdigest(),
        )
        historical_v2 = _historical_candidate_v2(_authority())
        self.assertTrue(
            validate_candidate_revision_v2(
                historical_v2.document,
                repo_root=REPO_ROOT,
            ).ok
        )
        candidate = _base_candidate(_authority())
        current_schema_path = REPO_ROOT / CANDIDATE_REVISION_V4_SCHEMA_REPO_PATH
        Draft202012Validator.check_schema(
            json.loads(current_schema_path.read_text(encoding="utf-8"))
        )
        self.assertEqual(
            candidate_schema_digest_v4(REPO_ROOT),
            hashlib.sha256(current_schema_path.read_bytes()).hexdigest(),
        )
        self.assertTrue(validate_candidate_revision_v4(candidate.document).ok)
        self.assertEqual(candidate.document["schema_version"], 4)
        self.assertEqual(candidate.document["authority_class"], "candidate_only")
        self.assertEqual(candidate.document["canonical_effect"], "none")
        for forbidden in (
            "actor",
            "created_at",
            "revision",
            "previous_revision",
            "session_id",
            "attempt_id",
            "readiness",
            "score",
            "budget",
        ):
            self.assertNotIn(forbidden, candidate.document)
        self.assertEqual(
            candidate.document["provenance"]["kind"],
            "native_executive_epoch",
        )
        self.assertNotIn("formal_provenance", candidate.document)

    def test_reference_order_and_new_epoch_do_not_create_meaning(self) -> None:
        first_authority = _authority(epoch_id="epoch.candidate.1")
        evidence_ref = {
            "kind": "evidence",
            "id": "evidence.transfer.1",
            "revision": 2,
            "digest_sha256": "1" * 64,
        }
        branch_ref = {
            "kind": "branch",
            "id": "branch.transfer.1",
            "revision": 3,
            "digest_sha256": "2" * 64,
        }
        first = _base_candidate(
            first_authority,
            supporting_refs=(branch_ref, evidence_ref),
            argument_edges=(
                {
                    "edge_id": "edge.transfer.1",
                    "edge_kind": "argument",
                    "premises": ["core invariance", "fixed normalization"],
                    "conclusion": "the transfer stays on the core",
                    "authority": {
                        "class": "evidence_interpretation",
                        "refs": [branch_ref, evidence_ref],
                    },
                },
            ),
        )
        reordered = _base_candidate(
            first_authority,
            supporting_refs=(evidence_ref, branch_ref),
            argument_edges=(
                {
                    "edge_id": "edge.transfer.1",
                    "edge_kind": "argument",
                    "premises": ["fixed normalization", "core invariance"],
                    "conclusion": "the transfer stays on the core",
                    "authority": {
                        "class": "evidence_interpretation",
                        "refs": [evidence_ref, branch_ref],
                    },
                },
            ),
        )
        self.assertEqual(first.digest_sha256, reordered.digest_sha256)

        persisted = _persisted(first)
        successor_epoch = _authority(epoch_id="epoch.candidate.2")
        same = _base_candidate(
            successor_epoch,
            predecessor=persisted,
            supporting_refs=(evidence_ref, branch_ref),
            argument_edges=(
                {
                    "edge_id": "edge.transfer.1",
                    "edge_kind": "argument",
                    "premises": ["fixed normalization", "core invariance"],
                    "conclusion": "the transfer stays on the core",
                    "authority": {
                        "class": "evidence_interpretation",
                        "refs": [evidence_ref, branch_ref],
                    },
                },
            ),
        )
        self.assertEqual(same.digest_sha256, first.digest_sha256)
        self.assertEqual(same.document["provenance"], first.document["provenance"])

    def test_same_identity_rejects_core_change_and_new_identity_requires_link(
        self,
    ) -> None:
        authority = _authority()
        first = _base_candidate(authority)
        persisted = _persisted(first)
        changes = {
            "exact_statement": "A materially different transfer assertion.",
            "mechanism": "A new resolvent mechanism.",
            "hypotheses": ("a different domain hypothesis",),
            "domain": "a different operator domain",
            "normalization": ("a different normalization",),
            "scope_and_reach": "a different theorem scope",
        }
        for field_name, changed_value in changes.items():
            with (
                self.subTest(field=field_name),
                self.assertRaisesRegex(
                    ValueError,
                    "linked new identity",
                ),
            ):
                _base_candidate(
                    authority,
                    predecessor=persisted,
                    **{field_name: changed_value},
                )

        revised_basis = _base_candidate(
            authority,
            predecessor=persisted,
            gaps=("the closure argument still has one exact unresolved step",),
            objections=("the current estimate may not survive completion",),
        )
        self.assertEqual(
            revised_basis.document["candidate_id"], first.document["candidate_id"]
        )
        self.assertNotEqual(revised_basis.digest_sha256, first.digest_sha256)

        with self.assertRaisesRegex(ValueError, "exact genealogy link"):
            _base_candidate(
                authority,
                candidate_id="candidate.theta-transfer.2",
                predecessor=persisted,
                exact_statement="A materially repaired transfer assertion.",
            )
        link = {
            "relation": "repair_of",
            "candidate_ref": {
                "candidate_id": first.document["candidate_id"],
                "revision": persisted.revision,
                "digest_sha256": persisted.payload_digest,
            },
        }
        repaired = _base_candidate(
            authority,
            candidate_id="candidate.theta-transfer.2",
            predecessor=persisted,
            exact_statement="A materially repaired transfer assertion.",
            genealogy=(link,),
        )
        self.assertEqual(repaired.document["genealogy"][0], link)

    def test_branch_citations_are_zero_or_many_and_formal_provenance_is_selected(
        self,
    ) -> None:
        authority = _authority()
        branch_refs = (
            {
                "kind": "branch",
                "id": "branch.transfer.2",
                "revision": 1,
                "digest_sha256": "3" * 64,
            },
            {
                "kind": "branch",
                "id": "branch.transfer.1",
                "revision": 4,
                "digest_sha256": "4" * 64,
            },
        )
        native = _base_candidate(authority)
        self.assertNotIn("supporting_refs", native.document)

        formal = _base_candidate(
            authority,
            candidate_id="candidate.formal-transfer.1",
            supporting_refs=branch_refs,
            provenance={
                "kind": "formal_attempt",
                "session_ref": {
                    "session_id": "session.transfer.1",
                    "revision": 2,
                    "digest_sha256": "5" * 64,
                },
                "attempt_ref": {
                    "attempt_id": "attempt.transfer.1",
                    "digest_sha256": "6" * 64,
                },
            },
        )
        self.assertEqual(
            [row["kind"] for row in formal.document["supporting_refs"]],
            ["branch", "branch"],
        )
        self.assertEqual(formal.document["provenance"]["kind"], "formal_attempt")
        self.assertNotIn("executive_epoch_id", formal.document["provenance"])

    def test_commit_forwards_exact_a1_requirement_and_read_reissues_v4(self) -> None:
        authority = _authority()
        store = _CandidateStoreFake()
        ordinary = _base_candidate(authority)
        commit_candidate_revision(
            store,
            authority=authority,
            record=ordinary,
            lease="lease.1",
            actor="coordinating-codex",
        )
        ordinary_call = store.commit_calls[-1]
        self.assertIsNone(ordinary_call["a1_requirement"])
        self.assertEqual(
            ordinary_call["expected_canonical_authority_digest"],
            authority.canonical_authority_digest,
        )
        self.assertEqual(ordinary_call["mission_id"], authority.mission_id)
        self.assertEqual(
            ordinary_call["executive_epoch_id"], authority.executive_epoch_id
        )
        self.assertEqual(ordinary_call["dependency_heads"], {})

        purported_complete = prepare_candidate_revision(
            authority=authority,
            candidate_id="candidate.rh.complete.1",
            proposal_kind="proof_architecture",
            exact_statement=(
                "Every nontrivial zero of the Riemann zeta function lies on the critical line."
            ),
            mechanism="A purported unconditional argument proves the exact statement.",
            scope_and_reach="complete proof of the Riemann Hypothesis",
            standing={
                "status": "open",
                "basis": "The purported complete argument requires adversarial review.",
            },
            complete_target_claim={
                "target": "riemann_hypothesis",
                "disposition": "proof",
            },
            repo_root=REPO_ROOT,
        )
        self.assertTrue(candidate_requires_a1(purported_complete.document))
        expected_requirement = {
            "kind": "candidate_a1_requirement",
            "classification": "purported_complete_rh_proof_or_disproof",
            "required_atomic_effect": "open_preservation_first_a1_hold",
        }
        self.assertEqual(
            deep_thaw(candidate_a1_requirement(purported_complete.document)),
            expected_requirement,
        )
        self.assertEqual(
            deep_thaw(candidate_complete_target_claim(purported_complete.document)),
            {
                "target": "riemann_hypothesis",
                "disposition": "proof",
            },
        )
        commit_candidate_revision(
            store,
            authority=authority,
            record=purported_complete,
            lease="lease.1",
            actor="coordinating-codex",
            expected_head_revision=1,
            expected_head_payload_digest="7" * 64,
            command_id="candidate.rh.complete.commit.1",
        )
        complete_call = store.commit_calls[-1]
        self.assertEqual(
            deep_thaw(complete_call["a1_requirement"]), expected_requirement
        )
        self.assertEqual(complete_call["payload"], purported_complete.document)
        self.assertNotIn("alert", complete_call)
        self.assertNotIn("evidence", complete_call)

        purported_disproof = prepare_candidate_revision(
            authority=authority,
            candidate_id="candidate.rh.disproof.1",
            proposal_kind="proof_architecture",
            exact_statement="The stated contradiction disproves the Riemann Hypothesis.",
            mechanism="A purported global contradiction rules out RH.",
            scope_and_reach="complete disproof of the Riemann Hypothesis",
            standing={
                "status": "open",
                "basis": "The purported disproof requires adversarial review.",
            },
            complete_target_claim={
                "target": "riemann_hypothesis",
                "disposition": "disproof",
            },
            repo_root=REPO_ROOT,
        )
        self.assertTrue(candidate_requires_a1(purported_disproof.document))

        store.rows[ordinary.document["candidate_id"]] = [
            {
                "payload": deep_thaw(ordinary.document),
                "revision": 1,
                "payload_digest": ordinary.digest_sha256,
                "predecessor_revision": None,
                "created_actor": "coordinating-codex",
                "created_at": "2026-08-23T12:00:00Z",
            }
        ]
        readback = read_candidate_revision(
            store,
            mission_id="mission.1",
            candidate_id=str(ordinary.document["candidate_id"]),
        )
        self.assertIsInstance(readback, PersistedCandidateRevisionV4)
        self.assertEqual(readback.record.document, ordinary.document)
        self.assertEqual(readback.record.digest_sha256, ordinary.digest_sha256)
        self.assertEqual(
            canonical_json_bytes(readback.record.document),
            canonical_json_bytes(ordinary.document),
        )
        self.assertTrue(readback.is_current_head)

    def test_commit_rejects_forged_native_epoch_provenance_before_store_write(
        self,
    ) -> None:
        authority = _authority()
        store = _CandidateStoreFake()
        forged = _base_candidate(
            authority,
            candidate_id="candidate.forged-provenance.1",
            provenance={
                "kind": "native_executive_epoch",
                "executive_epoch_id": authority.executive_epoch_id,
                "executive_epoch_authority_sha256": "f" * 64,
            },
        )

        with self.assertRaisesRegex(
            ValueError,
            "native provenance differs from the active Store epoch binding",
        ):
            commit_candidate_revision(
                store,
                authority=authority,
                record=forged,
                lease="lease.1",
                actor="coordinating-codex",
            )
        self.assertEqual(store.commit_calls, [])


if __name__ == "__main__":
    unittest.main()
