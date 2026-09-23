"""Proof-neutral independent triage for one exact Candidate A1 revision.

This module owns only the immutable semantic value used by the later Store and
Host integration.  It does not grant review authority, persist Evidence, alter
canonical mathematics, authorize publication, or resolve any other Candidate
revision.  One frozen Candidate A1 identity has one outcome-independent
Evidence identity and exactly one revision.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .evidence_store import EvidenceItemRevision
from .json_support import canonical_json_bytes
from .research_model import deep_thaw


CANDIDATE_A1_TRIAGE_EVIDENCE_SUBTYPE = "candidate_a1_triage"
CANDIDATE_A1_TRIAGE_INVALIDATED = "invalidated"
CANDIDATE_A1_TRIAGE_ADMISSION_READY = "admission_ready"
CANDIDATE_A1_TRIAGE_DISPOSITIONS = frozenset(
    {
        CANDIDATE_A1_TRIAGE_INVALIDATED,
        CANDIDATE_A1_TRIAGE_ADMISSION_READY,
    }
)
CANDIDATE_A1_TRIAGE_EVIDENCE_ID_PREFIX = "evidence:candidate-a1-triage:"
CANDIDATE_A1_REVIEWER_ROLE = "independent_candidate_a1_reviewer"

_TRIAGE_RIGOR = "independent_exact_candidate_a1_triage"
_TRIAGE_SECURITY_CLASSIFICATION = "workspace_internal"
_TRIAGE_RETENTION = "mission_candidate_a1_triage"
_CANONICAL_NON_INFERENCES = (
    "does not change canonical RH truth",
    "does not authorize public disclosure",
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}")


def _require_text(value: object, location: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{location} must be nonempty text")
    return value


def _require_positive_int(value: object, location: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{location} must be a positive integer")
    return value


def _require_sha256(value: object, location: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{location} must be lowercase SHA-256")
    return value


def _closed_payload(
    value: object,
    *,
    keys: frozenset[str],
    location: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError(f"{location} must have the exact closed shape")
    return value


def _normalize_texts(
    values: Sequence[str] | None,
    location: str,
) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, (str, bytes, bytearray)):
        raise ValueError(f"{location} must be an array")
    normalized = tuple(_require_text(value, location) for value in values)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{location} must not contain duplicates")
    return tuple(sorted(normalized))


@dataclass(frozen=True, slots=True, order=True)
class CandidateA1Ref:
    """Exact Mission-scoped identity of one frozen Candidate A1 revision."""

    mission_id: str
    candidate_id: str
    revision: int
    digest_sha256: str

    def __post_init__(self) -> None:
        _require_text(self.mission_id, "Candidate A1 mission_id")
        _require_text(self.candidate_id, "Candidate A1 candidate_id")
        _require_positive_int(self.revision, "Candidate A1 revision")
        _require_sha256(self.digest_sha256, "Candidate A1 digest")

    def to_payload(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "candidate_id": self.candidate_id,
            "revision": self.revision,
            "digest_sha256": self.digest_sha256,
        }

    @classmethod
    def from_payload(cls, value: object) -> CandidateA1Ref:
        payload = _closed_payload(
            value,
            keys=frozenset({"mission_id", "candidate_id", "revision", "digest_sha256"}),
            location="candidate_ref",
        )
        return cls(
            mission_id=payload["mission_id"],
            candidate_id=payload["candidate_id"],
            revision=payload["revision"],
            digest_sha256=payload["digest_sha256"],
        )


@dataclass(frozen=True, slots=True, order=True)
class EvidenceRevisionRef:
    """Exact Mission-scoped supporting Evidence revision."""

    mission_id: str
    evidence_id: str
    revision: int
    digest_sha256: str

    def __post_init__(self) -> None:
        _require_text(self.mission_id, "Evidence ref mission_id")
        _require_text(self.evidence_id, "Evidence ref evidence_id")
        _require_positive_int(self.revision, "Evidence ref revision")
        _require_sha256(self.digest_sha256, "Evidence ref digest")

    def to_payload(self) -> dict[str, Any]:
        return {
            "mission_id": self.mission_id,
            "evidence_id": self.evidence_id,
            "revision": self.revision,
            "digest_sha256": self.digest_sha256,
        }

    @classmethod
    def from_payload(cls, value: object) -> EvidenceRevisionRef:
        payload = _closed_payload(
            value,
            keys=frozenset({"mission_id", "evidence_id", "revision", "digest_sha256"}),
            location="Evidence ref",
        )
        return cls(
            mission_id=payload["mission_id"],
            evidence_id=payload["evidence_id"],
            revision=payload["revision"],
            digest_sha256=payload["digest_sha256"],
        )


@dataclass(frozen=True, slots=True)
class CandidateA1ReviewerProvenance:
    """Caller-bound reviewer facts for later Store/Host authority validation."""

    reviewer_identity: str
    reviewer_thread_id: str
    root_thread_id: str
    parent_thread_id: str
    assignment_id: str
    context_id: str
    context_revision: int
    context_digest_sha256: str
    grant_id: str
    grant_digest_sha256: str
    reviewer_role: str = CANDIDATE_A1_REVIEWER_ROLE

    def __post_init__(self) -> None:
        for location, value in (
            ("reviewer identity", self.reviewer_identity),
            ("reviewer thread", self.reviewer_thread_id),
            ("root thread", self.root_thread_id),
            ("parent thread", self.parent_thread_id),
            ("assignment id", self.assignment_id),
            ("review Context id", self.context_id),
            ("review grant id", self.grant_id),
        ):
            _require_text(value, location)
        _require_positive_int(self.context_revision, "review Context revision")
        _require_sha256(self.context_digest_sha256, "review Context digest")
        _require_sha256(self.grant_digest_sha256, "review grant digest")
        if self.reviewer_role != CANDIDATE_A1_REVIEWER_ROLE:
            raise ValueError(
                "Candidate A1 triage requires the independent reviewer role"
            )
        if self.parent_thread_id != self.root_thread_id:
            raise ValueError("Candidate A1 reviewer must be a direct child of the root")
        if self.reviewer_thread_id == self.root_thread_id:
            raise ValueError(
                "Candidate A1 reviewer must be role-disjoint from the root"
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "reviewer_role": self.reviewer_role,
            "reviewer_identity": self.reviewer_identity,
            "reviewer_thread_id": self.reviewer_thread_id,
            "child_lineage": {
                "root_thread_id": self.root_thread_id,
                "parent_thread_id": self.parent_thread_id,
                "assignment_id": self.assignment_id,
            },
            "context_ref": {
                "context_id": self.context_id,
                "revision": self.context_revision,
                "digest_sha256": self.context_digest_sha256,
            },
            "grant_ref": {
                "grant_id": self.grant_id,
                "digest_sha256": self.grant_digest_sha256,
            },
        }

    @classmethod
    def from_payload(cls, value: object) -> CandidateA1ReviewerProvenance:
        payload = _closed_payload(
            value,
            keys=frozenset(
                {
                    "reviewer_role",
                    "reviewer_identity",
                    "reviewer_thread_id",
                    "child_lineage",
                    "context_ref",
                    "grant_ref",
                }
            ),
            location="reviewer",
        )
        lineage = _closed_payload(
            payload["child_lineage"],
            keys=frozenset({"root_thread_id", "parent_thread_id", "assignment_id"}),
            location="reviewer.child_lineage",
        )
        context = _closed_payload(
            payload["context_ref"],
            keys=frozenset({"context_id", "revision", "digest_sha256"}),
            location="reviewer.context_ref",
        )
        grant = _closed_payload(
            payload["grant_ref"],
            keys=frozenset({"grant_id", "digest_sha256"}),
            location="reviewer.grant_ref",
        )
        return cls(
            reviewer_role=payload["reviewer_role"],
            reviewer_identity=payload["reviewer_identity"],
            reviewer_thread_id=payload["reviewer_thread_id"],
            root_thread_id=lineage["root_thread_id"],
            parent_thread_id=lineage["parent_thread_id"],
            assignment_id=lineage["assignment_id"],
            context_id=context["context_id"],
            context_revision=context["revision"],
            context_digest_sha256=context["digest_sha256"],
            grant_id=grant["grant_id"],
            grant_digest_sha256=grant["digest_sha256"],
        )


@dataclass(frozen=True, slots=True)
class CandidateA1ConcreteDefect:
    """One exact mathematical defect sufficient to defeat this complete claim."""

    exact_defect: str
    affected_scope: str
    sufficiency_basis: str

    def __post_init__(self) -> None:
        _require_text(self.exact_defect, "concrete defect")
        _require_text(self.affected_scope, "concrete defect scope")
        _require_text(self.sufficiency_basis, "concrete defect sufficiency basis")

    def to_payload(self) -> dict[str, Any]:
        return {
            "exact_defect": self.exact_defect,
            "affected_scope": self.affected_scope,
            "sufficiency_basis": self.sufficiency_basis,
        }

    @classmethod
    def from_payload(cls, value: object) -> CandidateA1ConcreteDefect:
        payload = _closed_payload(
            value,
            keys=frozenset(
                {
                    "exact_defect",
                    "affected_scope",
                    "sufficiency_basis",
                }
            ),
            location="concrete defect",
        )
        return cls(
            exact_defect=payload["exact_defect"],
            affected_scope=payload["affected_scope"],
            sufficiency_basis=payload["sufficiency_basis"],
        )


def candidate_a1_triage_evidence_id(
    *,
    candidate_id: str,
    candidate_revision: int,
    candidate_digest_sha256: str,
) -> str:
    """Derive the sole triage Evidence identity for one exact Candidate A1."""

    material = {
        "candidate_id": _require_text(candidate_id, "Candidate A1 candidate_id"),
        "revision": _require_positive_int(candidate_revision, "Candidate A1 revision"),
        "digest_sha256": _require_sha256(
            candidate_digest_sha256, "Candidate A1 digest"
        ),
    }
    digest = hashlib.sha256(canonical_json_bytes(material)).hexdigest()
    return f"{CANDIDATE_A1_TRIAGE_EVIDENCE_ID_PREFIX}{digest}"


def _expected_exact_scope(candidate_ref: CandidateA1Ref) -> str:
    return (
        "independent triage of exact Candidate A1 "
        f"{candidate_ref.candidate_id}@{candidate_ref.revision}:"
        f"{candidate_ref.digest_sha256}"
    )


def _normalize_evidence_refs(
    values: Sequence[EvidenceRevisionRef],
    *,
    mission_id: str,
    location: str,
) -> tuple[EvidenceRevisionRef, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise ValueError(f"{location} must be an array")
    refs = tuple(values)
    if any(type(item) is not EvidenceRevisionRef for item in refs):
        raise TypeError(f"{location} requires exact EvidenceRevisionRef values")
    if any(item.mission_id != mission_id for item in refs):
        raise ValueError(f"{location} must belong to the Candidate Mission")
    if len(refs) != len(set(refs)):
        raise ValueError(f"{location} must not contain duplicates")
    return tuple(sorted(refs))


def _normalize_defects(
    values: Sequence[CandidateA1ConcreteDefect],
) -> tuple[CandidateA1ConcreteDefect, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise ValueError("concrete_defects must be an array")
    defects = tuple(values)
    if any(type(item) is not CandidateA1ConcreteDefect for item in defects):
        raise TypeError(
            "concrete_defects requires exact CandidateA1ConcreteDefect values"
        )
    keyed = [(canonical_json_bytes(item.to_payload()), item) for item in defects]
    if len(keyed) != len({key for key, _ in keyed}):
        raise ValueError("concrete_defects must not contain duplicates")
    return tuple(item for _, item in sorted(keyed, key=lambda pair: pair[0]))


def _triage_subject_payload(
    *,
    candidate_ref: CandidateA1Ref,
    disposition: str,
    reviewer: CandidateA1ReviewerProvenance,
    review_finding: str,
    cited_basis: Sequence[EvidenceRevisionRef],
    concrete_defects: Sequence[CandidateA1ConcreteDefect],
    no_remaining_material_objection: bool,
    limitations: Sequence[str],
    non_inferences: Sequence[str],
) -> dict[str, Any]:
    return {
        "mission_id": candidate_ref.mission_id,
        "candidate_ref": candidate_ref.to_payload(),
        "disposition": disposition,
        "reviewer": reviewer.to_payload(),
        "review_finding": review_finding,
        "cited_basis": [item.to_payload() for item in cited_basis],
        "concrete_defects": [item.to_payload() for item in concrete_defects],
        "no_remaining_material_objection": no_remaining_material_objection,
        "limitations": list(limitations),
        "non_inferences": list(non_inferences),
        "canonical_effect": "none",
        "public_effect": "none",
    }


@dataclass(frozen=True, slots=True)
class CandidateA1TriageRecord:
    """One prepared, immutable and outcome-closed A1 triage Evidence value."""

    evidence: EvidenceItemRevision
    candidate_ref: CandidateA1Ref
    disposition: str
    reviewer: CandidateA1ReviewerProvenance
    review_finding: str
    cited_basis: tuple[EvidenceRevisionRef, ...]
    concrete_defects: tuple[CandidateA1ConcreteDefect, ...]
    no_remaining_material_objection: bool
    limitations: tuple[str, ...]
    non_inferences: tuple[str, ...]
    payload_digest: str
    canonical_effect: str = "none"
    public_effect: str = "none"

    def __post_init__(self) -> None:
        if type(self.evidence) is not EvidenceItemRevision:
            raise TypeError("Candidate A1 triage requires exact EvidenceItemRevision")
        if type(self.candidate_ref) is not CandidateA1Ref:
            raise TypeError("Candidate A1 triage requires exact CandidateA1Ref")
        if type(self.reviewer) is not CandidateA1ReviewerProvenance:
            raise TypeError("Candidate A1 triage requires exact reviewer provenance")
        _require_text(self.review_finding, "review finding")
        if self.disposition not in CANDIDATE_A1_TRIAGE_DISPOSITIONS:
            raise ValueError("unsupported Candidate A1 triage disposition")
        if type(self.no_remaining_material_objection) is not bool:
            raise TypeError("remaining-material-objection state must be boolean")
        if self.canonical_effect != "none" or self.public_effect != "none":
            raise ValueError("Candidate A1 triage cannot claim canonical/public effect")

        basis = _normalize_evidence_refs(
            self.cited_basis,
            mission_id=self.candidate_ref.mission_id,
            location="cited_basis",
        )
        defects = _normalize_defects(
            self.concrete_defects,
        )
        limitations = _normalize_texts(self.limitations, "limitations")
        non_inferences = _normalize_texts(self.non_inferences, "non_inferences")
        for statement in _CANONICAL_NON_INFERENCES:
            if statement not in non_inferences:
                raise ValueError(
                    "Candidate A1 triage must preserve canonical/public non-inferences"
                )
        if self.disposition == CANDIDATE_A1_TRIAGE_INVALIDATED:
            if not defects:
                raise ValueError("invalidated requires at least one concrete defect")
            if self.no_remaining_material_objection:
                raise ValueError("invalidated cannot state that no objection remains")
        else:
            if defects:
                raise ValueError("admission_ready cannot contain a concrete defect")
            if not self.no_remaining_material_objection:
                raise ValueError(
                    "admission_ready must state no remaining material objection"
                )
        object.__setattr__(self, "cited_basis", basis)
        object.__setattr__(self, "concrete_defects", defects)
        object.__setattr__(self, "limitations", limitations)
        object.__setattr__(self, "non_inferences", non_inferences)
        expected_subject = self.subject_payload()
        expected_id = candidate_a1_triage_evidence_id(
            candidate_id=self.candidate_ref.candidate_id,
            candidate_revision=self.candidate_ref.revision,
            candidate_digest_sha256=self.candidate_ref.digest_sha256,
        )
        if self.evidence.evidence_id != expected_id:
            raise ValueError("Candidate A1 triage Evidence identity is stale")
        if self.evidence.revision != 1:
            raise ValueError("Candidate A1 triage Evidence revision must be exactly 1")
        if self.evidence.subtype != CANDIDATE_A1_TRIAGE_EVIDENCE_SUBTYPE:
            raise ValueError("Candidate A1 triage uses the wrong Evidence subtype")
        if canonical_json_bytes(self.evidence.subject) != canonical_json_bytes(
            expected_subject
        ):
            raise ValueError("Candidate A1 triage Evidence subject is stale")
        if self.evidence.exact_scope != _expected_exact_scope(self.candidate_ref):
            raise ValueError("Candidate A1 triage Evidence exact_scope is stale")
        if self.evidence.rigor != _TRIAGE_RIGOR:
            raise ValueError("Candidate A1 triage Evidence rigor is stale")
        if self.evidence.limitations != limitations:
            raise ValueError("Candidate A1 triage Evidence limitations are stale")
        if self.evidence.non_inferences != non_inferences:
            raise ValueError("Candidate A1 triage Evidence non_inferences are stale")
        if (
            self.evidence.security_classification != _TRIAGE_SECURITY_CLASSIFICATION
            or self.evidence.retention != _TRIAGE_RETENTION
            or self.evidence.blob_roles
            or self.evidence.availability_state != "verified_available"
            or self.evidence.canonical_effect != "none"
        ):
            raise ValueError("Candidate A1 triage Evidence boundary is stale")
        expected_digest = hashlib.sha256(
            canonical_json_bytes(self.evidence.to_payload())
        ).hexdigest()
        if self.payload_digest != expected_digest:
            raise ValueError("Candidate A1 triage payload digest is stale")

    def subject_payload(self) -> dict[str, Any]:
        return _triage_subject_payload(
            candidate_ref=self.candidate_ref,
            disposition=self.disposition,
            reviewer=self.reviewer,
            review_finding=self.review_finding,
            cited_basis=self.cited_basis,
            concrete_defects=self.concrete_defects,
            no_remaining_material_objection=(self.no_remaining_material_objection),
            limitations=self.limitations,
            non_inferences=self.non_inferences,
        )

    def verify_prepared(self) -> None:
        if type(self) is not CandidateA1TriageRecord:
            raise ValueError("Candidate A1 triage subclasses have no authority")
        self.__post_init__()


def prepare_candidate_a1_triage(
    *,
    candidate_ref: CandidateA1Ref,
    disposition: str,
    reviewer: CandidateA1ReviewerProvenance,
    review_finding: str,
    cited_basis: Sequence[EvidenceRevisionRef] = (),
    concrete_defects: Sequence[CandidateA1ConcreteDefect] = (),
    no_remaining_material_objection: bool,
    limitations: Sequence[str] = (),
    non_inferences: Sequence[str] = (),
) -> CandidateA1TriageRecord:
    """Prepare one inert triage value for a frozen Candidate A1 revision."""

    if type(candidate_ref) is not CandidateA1Ref:
        raise TypeError("candidate_ref requires exact CandidateA1Ref")
    if type(reviewer) is not CandidateA1ReviewerProvenance:
        raise TypeError("reviewer requires exact CandidateA1ReviewerProvenance")
    defects = _normalize_defects(
        concrete_defects,
    )
    supplied_basis = _normalize_evidence_refs(
        cited_basis,
        mission_id=candidate_ref.mission_id,
        location="cited_basis",
    )
    basis = supplied_basis
    normalized_limitations = _normalize_texts(limitations, "limitations")
    normalized_non_inferences = _normalize_texts(non_inferences, "non_inferences")
    normalized_non_inferences = tuple(
        sorted({*normalized_non_inferences, *_CANONICAL_NON_INFERENCES})
    )
    normalized_finding = _require_text(review_finding, "review finding")
    subject = _triage_subject_payload(
        candidate_ref=candidate_ref,
        disposition=disposition,
        reviewer=reviewer,
        review_finding=normalized_finding,
        cited_basis=basis,
        concrete_defects=defects,
        no_remaining_material_objection=no_remaining_material_objection,
        limitations=normalized_limitations,
        non_inferences=normalized_non_inferences,
    )
    evidence = EvidenceItemRevision(
        evidence_id=candidate_a1_triage_evidence_id(
            candidate_id=candidate_ref.candidate_id,
            candidate_revision=candidate_ref.revision,
            candidate_digest_sha256=candidate_ref.digest_sha256,
        ),
        revision=1,
        subtype=CANDIDATE_A1_TRIAGE_EVIDENCE_SUBTYPE,
        subject=subject,
        exact_scope=_expected_exact_scope(candidate_ref),
        rigor=_TRIAGE_RIGOR,
        limitations=normalized_limitations,
        non_inferences=normalized_non_inferences,
        security_classification=_TRIAGE_SECURITY_CLASSIFICATION,
        retention=_TRIAGE_RETENTION,
        blob_roles=(),
        availability_state="verified_available",
    )
    return CandidateA1TriageRecord(
        evidence=evidence,
        candidate_ref=candidate_ref,
        disposition=disposition,
        reviewer=reviewer,
        review_finding=normalized_finding,
        cited_basis=basis,
        concrete_defects=defects,
        no_remaining_material_objection=no_remaining_material_objection,
        limitations=normalized_limitations,
        non_inferences=normalized_non_inferences,
        payload_digest=hashlib.sha256(
            canonical_json_bytes(evidence.to_payload())
        ).hexdigest(),
    )


def candidate_a1_triage_from_evidence(
    evidence: EvidenceItemRevision,
) -> CandidateA1TriageRecord:
    """Reconstruct and validate the reserved triage subtype from exact Evidence."""

    if type(evidence) is not EvidenceItemRevision:
        raise TypeError("candidate_a1_triage_from_evidence requires exact Evidence")
    if evidence.subtype != CANDIDATE_A1_TRIAGE_EVIDENCE_SUBTYPE:
        raise ValueError("Evidence is not Candidate A1 triage")
    subject = _closed_payload(
        deep_thaw(evidence.subject),
        keys=frozenset(
            {
                "mission_id",
                "candidate_ref",
                "disposition",
                "reviewer",
                "review_finding",
                "cited_basis",
                "concrete_defects",
                "no_remaining_material_objection",
                "limitations",
                "non_inferences",
                "canonical_effect",
                "public_effect",
            }
        ),
        location="Candidate A1 triage subject",
    )
    candidate_ref = CandidateA1Ref.from_payload(subject["candidate_ref"])
    if subject["mission_id"] != candidate_ref.mission_id:
        raise ValueError("triage subject Mission differs from Candidate ref")
    if subject["canonical_effect"] != "none" or subject["public_effect"] != "none":
        raise ValueError("Candidate A1 triage subject cannot claim an effect")
    cited_basis = subject["cited_basis"]
    defects = subject["concrete_defects"]
    for values, location in (
        (cited_basis, "cited_basis"),
        (defects, "concrete_defects"),
        (subject["limitations"], "limitations"),
        (subject["non_inferences"], "non_inferences"),
    ):
        if not isinstance(values, Sequence) or isinstance(
            values, (str, bytes, bytearray)
        ):
            raise ValueError(f"{location} must be an array")
    return CandidateA1TriageRecord(
        evidence=evidence,
        candidate_ref=candidate_ref,
        disposition=subject["disposition"],
        reviewer=CandidateA1ReviewerProvenance.from_payload(subject["reviewer"]),
        review_finding=subject["review_finding"],
        cited_basis=tuple(
            EvidenceRevisionRef.from_payload(item) for item in cited_basis
        ),
        concrete_defects=tuple(
            CandidateA1ConcreteDefect.from_payload(item) for item in defects
        ),
        no_remaining_material_objection=subject["no_remaining_material_objection"],
        limitations=tuple(subject["limitations"]),
        non_inferences=tuple(subject["non_inferences"]),
        payload_digest=hashlib.sha256(
            canonical_json_bytes(evidence.to_payload())
        ).hexdigest(),
    )
