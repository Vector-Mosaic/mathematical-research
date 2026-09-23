"""Proof-neutral Admission values for one frozen complete-target Candidate.

The three immutable Evidence subtypes in this module are the Mathematical
Research-owned Admission plane.  They preserve an exact case, one independent
review, and one role-disjoint decision.  None of them changes canonical truth,
Strategy, Mission lifecycle, or public state.  An ``authorize_exact_delta``
decision only prepares the exact symmetric result binding consumed by the
separately authorized canonical writer.  A ``reject`` decision carries the
concrete material objections that defeat its exact frozen claim and resolves
only that Candidate A1 as independently invalidated; it does not rewrite the
Candidate or its immutable triage history.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from .candidate_a1_triage import CandidateA1Ref
from .evidence_store import EvidenceItemRevision
from .json_support import canonical_json_bytes
from .research_model import deep_thaw


ADMISSION_CASE_EVIDENCE_SUBTYPE = "complete_claim_admission_case"
ADMISSION_REVIEW_EVIDENCE_SUBTYPE = "complete_claim_admission_review"
ADMISSION_DECISION_EVIDENCE_SUBTYPE = "complete_claim_admission_decision"
ADMISSION_CASE_EVIDENCE_ID_PREFIX = "evidence:complete-claim-admission-case:"
ADMISSION_REVIEW_EVIDENCE_ID_PREFIX = "evidence:complete-claim-admission-review:"
ADMISSION_DECISION_EVIDENCE_ID_PREFIX = "evidence:complete-claim-admission-decision:"

ADMISSION_REVIEWER_ROLE = "independent_complete_claim_admission_reviewer"
ADMISSION_ADMITTER_ROLE = "independent_complete_claim_admitter"
ADMISSION_REVIEW_NO_MATERIAL_OBJECTION = "no_material_objection"
ADMISSION_REVIEW_MATERIAL_OBJECTION = "material_objection"
ADMISSION_REVIEW_DISPOSITIONS = frozenset(
    {ADMISSION_REVIEW_NO_MATERIAL_OBJECTION, ADMISSION_REVIEW_MATERIAL_OBJECTION}
)
ADMISSION_DECISION_AUTHORIZE = "authorize_exact_delta"
ADMISSION_DECISION_REJECT = "reject"
ADMISSION_DECISION_DISPOSITIONS = frozenset(
    {ADMISSION_DECISION_AUTHORIZE, ADMISSION_DECISION_REJECT}
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_BASE_NON_INFERENCES = (
    "does not change canonical RH truth",
    "does not change Mission or Strategy state",
    "does not authorize public disclosure",
)
_CASE_REVIEW_NON_INFERENCES = (
    *_BASE_NON_INFERENCES,
    "does not change Candidate A1 state",
)
_DECISION_NON_INFERENCES = (
    *_BASE_NON_INFERENCES,
    "does not rewrite immutable Candidate A1 triage history",
)
_SECURITY = "workspace_internal"
_RETENTION = "mission_complete_claim_admission"


def _text(value: object, location: str) -> str:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{location} must be nonempty text")
    return value


def _positive(value: object, location: str) -> int:
    if type(value) is not int or value < 1:
        raise ValueError(f"{location} must be a positive integer")
    return value


def _digest(value: object, location: str) -> str:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        raise ValueError(f"{location} must be lowercase SHA-256")
    return value


def _closed(value: object, keys: frozenset[str], location: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError(f"{location} must have the exact closed shape")
    return value


def _texts(values: Sequence[str], location: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise ValueError(f"{location} must be an array")
    normalized = tuple(_text(item, location) for item in values)
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{location} must not contain duplicates")
    return tuple(sorted(normalized))


@dataclass(frozen=True, slots=True, order=True)
class AdmissionOwnerRevisionRef:
    kind: str
    identity: str
    revision: int
    payload_sha256: str

    def __post_init__(self) -> None:
        if self.kind not in {"evidence", "context", "branch", "candidate"}:
            raise ValueError("Admission basis kind is unsupported")
        _text(self.identity, "Admission basis identity")
        _positive(self.revision, "Admission basis revision")
        _digest(self.payload_sha256, "Admission basis digest")

    def to_payload(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "identity": self.identity,
            "revision": self.revision,
            "payload_sha256": self.payload_sha256,
        }

    @classmethod
    def from_payload(cls, value: object) -> "AdmissionOwnerRevisionRef":
        payload = _closed(
            value,
            frozenset({"kind", "identity", "revision", "payload_sha256"}),
            "Admission basis ref",
        )
        return cls(
            kind=payload["kind"],
            identity=payload["identity"],
            revision=payload["revision"],
            payload_sha256=payload["payload_sha256"],
        )


@dataclass(frozen=True, slots=True, order=True)
class AdmissionEvidenceRef:
    evidence_id: str
    revision: int
    payload_sha256: str

    def __post_init__(self) -> None:
        _text(self.evidence_id, "Admission Evidence identity")
        _positive(self.revision, "Admission Evidence revision")
        _digest(self.payload_sha256, "Admission Evidence digest")

    def to_payload(self) -> dict[str, Any]:
        return {
            "evidence_id": self.evidence_id,
            "revision": self.revision,
            "payload_sha256": self.payload_sha256,
        }

    @classmethod
    def from_payload(cls, value: object) -> "AdmissionEvidenceRef":
        payload = _closed(
            value,
            frozenset({"evidence_id", "revision", "payload_sha256"}),
            "Admission Evidence ref",
        )
        return cls(
            evidence_id=payload["evidence_id"],
            revision=payload["revision"],
            payload_sha256=payload["payload_sha256"],
        )


@dataclass(frozen=True, slots=True)
class AdmissionActorProvenance:
    role: str
    identity: str
    thread_id: str
    root_thread_id: str
    parent_thread_id: str
    assignment_id: str
    context_id: str
    context_revision: int
    context_digest_sha256: str
    grant_id: str
    grant_digest_sha256: str

    def __post_init__(self) -> None:
        if self.role not in {ADMISSION_REVIEWER_ROLE, ADMISSION_ADMITTER_ROLE}:
            raise ValueError("Admission actor role is unsupported")
        for location, value in (
            ("Admission actor identity", self.identity),
            ("Admission actor thread", self.thread_id),
            ("Admission root thread", self.root_thread_id),
            ("Admission parent thread", self.parent_thread_id),
            ("Admission assignment", self.assignment_id),
            ("Admission Context", self.context_id),
            ("Admission grant", self.grant_id),
        ):
            _text(value, location)
        _positive(self.context_revision, "Admission Context revision")
        _digest(self.context_digest_sha256, "Admission Context digest")
        _digest(self.grant_digest_sha256, "Admission grant digest")
        if self.parent_thread_id != self.root_thread_id:
            raise ValueError("Admission actor must be a direct child of the root")
        if self.thread_id == self.root_thread_id:
            raise ValueError("Admission actor must be role-disjoint from the root")

    def to_payload(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "identity": self.identity,
            "thread_id": self.thread_id,
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
    def from_payload(cls, value: object) -> "AdmissionActorProvenance":
        payload = _closed(
            value,
            frozenset(
                {
                    "role",
                    "identity",
                    "thread_id",
                    "child_lineage",
                    "context_ref",
                    "grant_ref",
                }
            ),
            "Admission actor",
        )
        lineage = _closed(
            payload["child_lineage"],
            frozenset({"root_thread_id", "parent_thread_id", "assignment_id"}),
            "Admission actor lineage",
        )
        context = _closed(
            payload["context_ref"],
            frozenset({"context_id", "revision", "digest_sha256"}),
            "Admission actor Context",
        )
        grant = _closed(
            payload["grant_ref"],
            frozenset({"grant_id", "digest_sha256"}),
            "Admission actor grant",
        )
        return cls(
            role=payload["role"],
            identity=payload["identity"],
            thread_id=payload["thread_id"],
            root_thread_id=lineage["root_thread_id"],
            parent_thread_id=lineage["parent_thread_id"],
            assignment_id=lineage["assignment_id"],
            context_id=context["context_id"],
            context_revision=context["revision"],
            context_digest_sha256=context["digest_sha256"],
            grant_id=grant["grant_id"],
            grant_digest_sha256=grant["digest_sha256"],
        )


@dataclass(frozen=True, slots=True, order=True)
class AdmissionMaterialObjection:
    exact_objection: str
    affected_scope: str
    materiality_basis: str

    def __post_init__(self) -> None:
        _text(self.exact_objection, "Admission objection")
        _text(self.affected_scope, "Admission objection scope")
        _text(self.materiality_basis, "Admission objection materiality")

    def to_payload(self) -> dict[str, str]:
        return {
            "exact_objection": self.exact_objection,
            "affected_scope": self.affected_scope,
            "materiality_basis": self.materiality_basis,
        }

    @classmethod
    def from_payload(cls, value: object) -> "AdmissionMaterialObjection":
        payload = _closed(
            value,
            frozenset({"exact_objection", "affected_scope", "materiality_basis"}),
            "Admission objection",
        )
        return cls(**payload)


def _evidence_id(prefix: str, material: Mapping[str, Any]) -> str:
    return prefix + hashlib.sha256(canonical_json_bytes(material)).hexdigest()


def admission_case_evidence_id(project_id: str, candidate_ref: CandidateA1Ref) -> str:
    return _evidence_id(
        ADMISSION_CASE_EVIDENCE_ID_PREFIX,
        {
            "project_id": _text(project_id, "project_id"),
            "candidate_ref": candidate_ref.to_payload(),
        },
    )


def admission_review_evidence_id(case_ref: AdmissionEvidenceRef) -> str:
    return _evidence_id(ADMISSION_REVIEW_EVIDENCE_ID_PREFIX, case_ref.to_payload())


def admission_decision_evidence_id(
    case_ref: AdmissionEvidenceRef, review_ref: AdmissionEvidenceRef
) -> str:
    return _evidence_id(
        ADMISSION_DECISION_EVIDENCE_ID_PREFIX,
        {"case_ref": case_ref.to_payload(), "review_ref": review_ref.to_payload()},
    )


def _normalized_owner_refs(
    values: Sequence[AdmissionOwnerRevisionRef],
    location: str = "source_closure",
) -> tuple[AdmissionOwnerRevisionRef, ...]:
    if isinstance(values, (str, bytes, bytearray)):
        raise ValueError(f"{location} must be an array")
    refs = tuple(values)
    if any(type(item) is not AdmissionOwnerRevisionRef for item in refs):
        raise TypeError(f"{location} requires exact AdmissionOwnerRevisionRef values")
    if len(refs) != len(set(refs)):
        raise ValueError(f"{location} must not contain duplicates")
    return tuple(sorted(refs))


def _record_evidence(
    *,
    evidence_id: str,
    subtype: str,
    subject: Mapping[str, Any],
    exact_scope: str,
    rigor: str,
    limitations: tuple[str, ...],
    non_inferences: tuple[str, ...],
) -> EvidenceItemRevision:
    return EvidenceItemRevision(
        evidence_id=evidence_id,
        revision=1,
        subtype=subtype,
        subject=subject,
        exact_scope=exact_scope,
        rigor=rigor,
        limitations=limitations,
        non_inferences=non_inferences,
        security_classification=_SECURITY,
        retention=_RETENTION,
        blob_roles=(),
        availability_state="verified_available",
    )


def _verify_record_evidence(
    evidence: EvidenceItemRevision,
    *,
    evidence_id: str,
    subtype: str,
    subject: Mapping[str, Any],
    exact_scope: str,
    rigor: str,
    limitations: tuple[str, ...],
    non_inferences: tuple[str, ...],
    payload_digest: str,
) -> None:
    if evidence.evidence_id != evidence_id or evidence.revision != 1:
        raise ValueError("Admission Evidence identity or revision is stale")
    if evidence.subtype != subtype or canonical_json_bytes(
        evidence.subject
    ) != canonical_json_bytes(subject):
        raise ValueError("Admission Evidence subtype or subject is stale")
    if evidence.exact_scope != exact_scope or evidence.rigor != rigor:
        raise ValueError("Admission Evidence scope or rigor is stale")
    if evidence.limitations != limitations or evidence.non_inferences != non_inferences:
        raise ValueError("Admission Evidence limitations are stale")
    if (
        evidence.security_classification != _SECURITY
        or evidence.retention != _RETENTION
        or evidence.blob_roles
        or evidence.availability_state != "verified_available"
        or evidence.canonical_effect != "none"
    ):
        raise ValueError("Admission Evidence boundary is stale")
    expected = hashlib.sha256(canonical_json_bytes(evidence.to_payload())).hexdigest()
    if payload_digest != expected:
        raise ValueError("Admission Evidence payload digest is stale")


@dataclass(frozen=True, slots=True)
class AdmissionCaseRecord:
    evidence: EvidenceItemRevision
    project_id: str
    candidate_ref: CandidateA1Ref
    triage_ref: AdmissionEvidenceRef
    candidate_disposition: str
    exact_claim: str
    candidate_author_thread_id: str
    triage_reviewer_thread_id: str
    source_closure: tuple[AdmissionOwnerRevisionRef, ...]
    limitations: tuple[str, ...]
    non_inferences: tuple[str, ...]
    payload_digest: str

    def __post_init__(self) -> None:
        if self.candidate_disposition not in {"proof", "disproof"}:
            raise ValueError(
                "Admission Candidate disposition must be proof or disproof"
            )
        for location, value in (
            ("project_id", self.project_id),
            ("exact claim", self.exact_claim),
            ("Candidate author thread", self.candidate_author_thread_id),
            ("triage reviewer thread", self.triage_reviewer_thread_id),
        ):
            _text(value, location)
        closure = _normalized_owner_refs(self.source_closure)
        limitations = _texts(self.limitations, "limitations")
        non_inferences = _texts(self.non_inferences, "non_inferences")
        if any(item not in non_inferences for item in _CASE_REVIEW_NON_INFERENCES):
            raise ValueError("Admission Case must preserve all non-inferences")
        object.__setattr__(self, "source_closure", closure)
        object.__setattr__(self, "limitations", limitations)
        object.__setattr__(self, "non_inferences", non_inferences)
        _verify_record_evidence(
            self.evidence,
            evidence_id=admission_case_evidence_id(self.project_id, self.candidate_ref),
            subtype=ADMISSION_CASE_EVIDENCE_SUBTYPE,
            subject=self.subject_payload(),
            exact_scope=f"Admission Case for exact Candidate {self.candidate_ref.candidate_id}@{self.candidate_ref.revision}:{self.candidate_ref.digest_sha256}",
            rigor="exact_admission_case_freeze",
            limitations=limitations,
            non_inferences=non_inferences,
            payload_digest=self.payload_digest,
        )

    def subject_payload(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "mission_id": self.candidate_ref.mission_id,
            "candidate_ref": self.candidate_ref.to_payload(),
            "triage_ref": self.triage_ref.to_payload(),
            "candidate_disposition": self.candidate_disposition,
            "exact_claim": self.exact_claim,
            "candidate_author_thread_id": self.candidate_author_thread_id,
            "triage_reviewer_thread_id": self.triage_reviewer_thread_id,
            "source_closure": [item.to_payload() for item in self.source_closure],
            "canonical_effect": "none",
            "mathematical_effect": "none",
            "mission_effect": "none",
            "strategy_effect": "none",
            "public_effect": "none",
        }

    def verify_prepared(self) -> None:
        if type(self) is not AdmissionCaseRecord:
            raise ValueError("Admission Case subclasses have no authority")
        self.__post_init__()


def prepare_admission_case(
    *,
    project_id: str,
    candidate_ref: CandidateA1Ref,
    triage_ref: AdmissionEvidenceRef,
    candidate_disposition: str,
    exact_claim: str,
    candidate_author_thread_id: str,
    triage_reviewer_thread_id: str,
    source_closure: Sequence[AdmissionOwnerRevisionRef],
    limitations: Sequence[str] = (),
    non_inferences: Sequence[str] = (),
) -> AdmissionCaseRecord:
    closure = _normalized_owner_refs(source_closure)
    limits = _texts(limitations, "limitations")
    non = tuple(
        sorted(
            {
                *_texts(non_inferences, "non_inferences"),
                *_CASE_REVIEW_NON_INFERENCES,
            }
        )
    )
    subject = {
        "project_id": project_id,
        "mission_id": candidate_ref.mission_id,
        "candidate_ref": candidate_ref.to_payload(),
        "triage_ref": triage_ref.to_payload(),
        "candidate_disposition": candidate_disposition,
        "exact_claim": exact_claim,
        "candidate_author_thread_id": candidate_author_thread_id,
        "triage_reviewer_thread_id": triage_reviewer_thread_id,
        "source_closure": [item.to_payload() for item in closure],
        "canonical_effect": "none",
        "mathematical_effect": "none",
        "mission_effect": "none",
        "strategy_effect": "none",
        "public_effect": "none",
    }
    evidence = _record_evidence(
        evidence_id=admission_case_evidence_id(project_id, candidate_ref),
        subtype=ADMISSION_CASE_EVIDENCE_SUBTYPE,
        subject=subject,
        exact_scope=f"Admission Case for exact Candidate {candidate_ref.candidate_id}@{candidate_ref.revision}:{candidate_ref.digest_sha256}",
        rigor="exact_admission_case_freeze",
        limitations=limits,
        non_inferences=non,
    )
    return AdmissionCaseRecord(
        evidence=evidence,
        project_id=project_id,
        candidate_ref=candidate_ref,
        triage_ref=triage_ref,
        candidate_disposition=candidate_disposition,
        exact_claim=exact_claim,
        candidate_author_thread_id=candidate_author_thread_id,
        triage_reviewer_thread_id=triage_reviewer_thread_id,
        source_closure=closure,
        limitations=limits,
        non_inferences=non,
        payload_digest=hashlib.sha256(
            canonical_json_bytes(evidence.to_payload())
        ).hexdigest(),
    )


@dataclass(frozen=True, slots=True)
class AdmissionReviewRecord:
    evidence: EvidenceItemRevision
    candidate_ref: CandidateA1Ref
    case_ref: AdmissionEvidenceRef
    reviewer: AdmissionActorProvenance
    disposition: str
    review_finding: str
    objections: tuple[AdmissionMaterialObjection, ...]
    cited_basis: tuple[AdmissionOwnerRevisionRef, ...]
    limitations: tuple[str, ...]
    non_inferences: tuple[str, ...]
    payload_digest: str

    def __post_init__(self) -> None:
        if self.reviewer.role != ADMISSION_REVIEWER_ROLE:
            raise ValueError("Admission Review requires the reviewer role")
        if self.disposition not in ADMISSION_REVIEW_DISPOSITIONS:
            raise ValueError("unsupported Admission Review disposition")
        _text(self.review_finding, "Admission review finding")
        objections = tuple(sorted(self.objections))
        if any(type(item) is not AdmissionMaterialObjection for item in objections):
            raise TypeError("Admission objections require exact values")
        if self.disposition == ADMISSION_REVIEW_NO_MATERIAL_OBJECTION and objections:
            raise ValueError("no_material_objection cannot carry objections")
        if self.disposition == ADMISSION_REVIEW_MATERIAL_OBJECTION and not objections:
            raise ValueError("material_objection requires a concrete objection")
        cited = _normalized_owner_refs(self.cited_basis, "cited_basis")
        limits = _texts(self.limitations, "limitations")
        non = _texts(self.non_inferences, "non_inferences")
        if any(item not in non for item in _CASE_REVIEW_NON_INFERENCES):
            raise ValueError("Admission Review must preserve all non-inferences")
        object.__setattr__(self, "objections", objections)
        object.__setattr__(self, "cited_basis", cited)
        object.__setattr__(self, "limitations", limits)
        object.__setattr__(self, "non_inferences", non)
        _verify_record_evidence(
            self.evidence,
            evidence_id=admission_review_evidence_id(self.case_ref),
            subtype=ADMISSION_REVIEW_EVIDENCE_SUBTYPE,
            subject=self.subject_payload(),
            exact_scope=f"independent Admission Review of {self.case_ref.evidence_id}@1:{self.case_ref.payload_sha256}",
            rigor="independent_exact_complete_claim_admission_review",
            limitations=limits,
            non_inferences=non,
            payload_digest=self.payload_digest,
        )

    def subject_payload(self) -> dict[str, Any]:
        return {
            "mission_id": self.candidate_ref.mission_id,
            "candidate_ref": self.candidate_ref.to_payload(),
            "case_ref": self.case_ref.to_payload(),
            "reviewer": self.reviewer.to_payload(),
            "disposition": self.disposition,
            "review_finding": self.review_finding,
            "objections": [item.to_payload() for item in self.objections],
            "cited_basis": [item.to_payload() for item in self.cited_basis],
            "canonical_effect": "none",
            "mathematical_effect": "none",
            "mission_effect": "none",
            "strategy_effect": "none",
            "public_effect": "none",
        }

    def verify_prepared(self) -> None:
        if type(self) is not AdmissionReviewRecord:
            raise ValueError("Admission Review subclasses have no authority")
        self.__post_init__()


def prepare_admission_review(
    *,
    candidate_ref: CandidateA1Ref,
    case_ref: AdmissionEvidenceRef,
    reviewer: AdmissionActorProvenance,
    disposition: str,
    review_finding: str,
    objections: Sequence[AdmissionMaterialObjection] = (),
    cited_basis: Sequence[AdmissionOwnerRevisionRef] = (),
    limitations: Sequence[str] = (),
    non_inferences: Sequence[str] = (),
) -> AdmissionReviewRecord:
    obs = tuple(sorted(objections))
    cited = _normalized_owner_refs(cited_basis, "cited_basis")
    limits = _texts(limitations, "limitations")
    non = tuple(
        sorted(
            {
                *_texts(non_inferences, "non_inferences"),
                *_CASE_REVIEW_NON_INFERENCES,
            }
        )
    )
    subject = {
        "mission_id": candidate_ref.mission_id,
        "candidate_ref": candidate_ref.to_payload(),
        "case_ref": case_ref.to_payload(),
        "reviewer": reviewer.to_payload(),
        "disposition": disposition,
        "review_finding": review_finding,
        "objections": [item.to_payload() for item in obs],
        "cited_basis": [item.to_payload() for item in cited],
        "canonical_effect": "none",
        "mathematical_effect": "none",
        "mission_effect": "none",
        "strategy_effect": "none",
        "public_effect": "none",
    }
    evidence = _record_evidence(
        evidence_id=admission_review_evidence_id(case_ref),
        subtype=ADMISSION_REVIEW_EVIDENCE_SUBTYPE,
        subject=subject,
        exact_scope=f"independent Admission Review of {case_ref.evidence_id}@1:{case_ref.payload_sha256}",
        rigor="independent_exact_complete_claim_admission_review",
        limitations=limits,
        non_inferences=non,
    )
    return AdmissionReviewRecord(
        evidence=evidence,
        candidate_ref=candidate_ref,
        case_ref=case_ref,
        reviewer=reviewer,
        disposition=disposition,
        review_finding=review_finding,
        objections=obs,
        cited_basis=cited,
        limitations=limits,
        non_inferences=non,
        payload_digest=hashlib.sha256(
            canonical_json_bytes(evidence.to_payload())
        ).hexdigest(),
    )


@dataclass(frozen=True, slots=True)
class AdmissionDecisionRecord:
    evidence: EvidenceItemRevision
    candidate_ref: CandidateA1Ref
    case_ref: AdmissionEvidenceRef
    review_ref: AdmissionEvidenceRef
    admitter: AdmissionActorProvenance
    disposition: str
    decision_basis: str
    objections: tuple[AdmissionMaterialObjection, ...]
    cited_basis: tuple[AdmissionOwnerRevisionRef, ...]
    canonical_result_binding: Mapping[str, Any] | None
    limitations: tuple[str, ...]
    non_inferences: tuple[str, ...]
    payload_digest: str

    def __post_init__(self) -> None:
        if self.admitter.role != ADMISSION_ADMITTER_ROLE:
            raise ValueError("Admission Decision requires the admitter role")
        if self.disposition not in ADMISSION_DECISION_DISPOSITIONS:
            raise ValueError("unsupported Admission Decision disposition")
        _text(self.decision_basis, "Admission decision basis")
        objections = tuple(sorted(self.objections))
        if any(type(item) is not AdmissionMaterialObjection for item in objections):
            raise TypeError("Admission Decision objections require exact values")
        if self.disposition == ADMISSION_DECISION_AUTHORIZE and objections:
            raise ValueError("authorize_exact_delta cannot carry objections")
        if self.disposition == ADMISSION_DECISION_REJECT and not objections:
            raise ValueError(
                "reject requires a concrete material objection sufficient to "
                "defeat the exact frozen claim"
            )
        cited = _normalized_owner_refs(self.cited_basis, "cited_basis")
        if self.disposition == ADMISSION_DECISION_AUTHORIZE and cited:
            raise ValueError("authorize_exact_delta cannot carry cited basis")
        if self.disposition == ADMISSION_DECISION_REJECT and not cited:
            raise ValueError("reject requires exact Case-bounded cited basis")
        limits = _texts(self.limitations, "limitations")
        non = _texts(self.non_inferences, "non_inferences")
        if any(item not in non for item in _DECISION_NON_INFERENCES):
            raise ValueError("Admission Decision must preserve all non-inferences")
        binding = (
            None
            if self.canonical_result_binding is None
            else deep_thaw(self.canonical_result_binding)
        )
        if self.disposition == ADMISSION_DECISION_AUTHORIZE:
            if not isinstance(binding, Mapping) or set(binding) != {
                "target",
                "result",
                "candidate_disposition",
                "exact_claim",
                "candidate_ref",
                "case_ref",
                "review_ref",
            }:
                raise ValueError(
                    "authorize_exact_delta requires the exact canonical-result binding"
                )
            if binding["candidate_disposition"] not in {"proof", "disproof"}:
                raise ValueError("canonical-result Candidate disposition is invalid")
            expected_result = (
                "proved" if binding["candidate_disposition"] == "proof" else "disproved"
            )
            if (
                binding["target"] != "riemann_hypothesis"
                or binding["result"] != expected_result
                or binding["candidate_ref"] != self.candidate_ref.to_payload()
                or binding["case_ref"] != self.case_ref.to_payload()
                or binding["review_ref"] != self.review_ref.to_payload()
            ):
                raise ValueError("canonical-result binding is stale")
            _text(binding["exact_claim"], "canonical-result exact claim")
        elif binding is not None:
            raise ValueError(
                "only authorize_exact_delta may prepare a canonical-result binding"
            )
        object.__setattr__(
            self,
            "canonical_result_binding",
            None if binding is None else deep_thaw(binding),
        )
        object.__setattr__(self, "objections", objections)
        object.__setattr__(self, "cited_basis", cited)
        object.__setattr__(self, "limitations", limits)
        object.__setattr__(self, "non_inferences", non)
        _verify_record_evidence(
            self.evidence,
            evidence_id=admission_decision_evidence_id(self.case_ref, self.review_ref),
            subtype=ADMISSION_DECISION_EVIDENCE_SUBTYPE,
            subject=self.subject_payload(),
            exact_scope=f"role-disjoint Admission Decision for {self.case_ref.evidence_id}@1:{self.case_ref.payload_sha256}",
            rigor="role_disjoint_exact_complete_claim_admission_decision",
            limitations=limits,
            non_inferences=non,
            payload_digest=self.payload_digest,
        )

    def subject_payload(self) -> dict[str, Any]:
        return {
            "mission_id": self.candidate_ref.mission_id,
            "candidate_ref": self.candidate_ref.to_payload(),
            "case_ref": self.case_ref.to_payload(),
            "review_ref": self.review_ref.to_payload(),
            "admitter": self.admitter.to_payload(),
            "disposition": self.disposition,
            "decision_basis": self.decision_basis,
            "objections": [item.to_payload() for item in self.objections],
            "cited_basis": [item.to_payload() for item in self.cited_basis],
            "canonical_result_binding": deep_thaw(self.canonical_result_binding),
            "canonical_effect": "none",
            "mathematical_effect": "none",
            "mission_effect": "none",
            "strategy_effect": "none",
            "public_effect": "none",
        }

    def verify_prepared(self) -> None:
        if type(self) is not AdmissionDecisionRecord:
            raise ValueError("Admission Decision subclasses have no authority")
        self.__post_init__()


def prepare_admission_decision(
    *,
    candidate_ref: CandidateA1Ref,
    case_ref: AdmissionEvidenceRef,
    review_ref: AdmissionEvidenceRef,
    admitter: AdmissionActorProvenance,
    disposition: str,
    decision_basis: str,
    candidate_disposition: str,
    exact_claim: str,
    objections: Sequence[AdmissionMaterialObjection] = (),
    cited_basis: Sequence[AdmissionOwnerRevisionRef] = (),
    limitations: Sequence[str] = (),
    non_inferences: Sequence[str] = (),
) -> AdmissionDecisionRecord:
    binding = None
    if disposition == ADMISSION_DECISION_AUTHORIZE:
        if candidate_disposition not in {"proof", "disproof"}:
            raise ValueError("Candidate disposition must be proof or disproof")
        binding = {
            "target": "riemann_hypothesis",
            "result": "proved" if candidate_disposition == "proof" else "disproved",
            "candidate_disposition": candidate_disposition,
            "exact_claim": _text(exact_claim, "exact claim"),
            "candidate_ref": candidate_ref.to_payload(),
            "case_ref": case_ref.to_payload(),
            "review_ref": review_ref.to_payload(),
        }
    obs = tuple(sorted(objections))
    cited = _normalized_owner_refs(cited_basis, "cited_basis")
    limits = _texts(limitations, "limitations")
    non = tuple(
        sorted(
            {
                *_texts(non_inferences, "non_inferences"),
                *_DECISION_NON_INFERENCES,
            }
        )
    )
    subject = {
        "mission_id": candidate_ref.mission_id,
        "candidate_ref": candidate_ref.to_payload(),
        "case_ref": case_ref.to_payload(),
        "review_ref": review_ref.to_payload(),
        "admitter": admitter.to_payload(),
        "disposition": disposition,
        "decision_basis": decision_basis,
        "objections": [item.to_payload() for item in obs],
        "cited_basis": [item.to_payload() for item in cited],
        "canonical_result_binding": binding,
        "canonical_effect": "none",
        "mathematical_effect": "none",
        "mission_effect": "none",
        "strategy_effect": "none",
        "public_effect": "none",
    }
    evidence = _record_evidence(
        evidence_id=admission_decision_evidence_id(case_ref, review_ref),
        subtype=ADMISSION_DECISION_EVIDENCE_SUBTYPE,
        subject=subject,
        exact_scope=f"role-disjoint Admission Decision for {case_ref.evidence_id}@1:{case_ref.payload_sha256}",
        rigor="role_disjoint_exact_complete_claim_admission_decision",
        limitations=limits,
        non_inferences=non,
    )
    return AdmissionDecisionRecord(
        evidence=evidence,
        candidate_ref=candidate_ref,
        case_ref=case_ref,
        review_ref=review_ref,
        admitter=admitter,
        disposition=disposition,
        decision_basis=decision_basis,
        objections=obs,
        cited_basis=cited,
        canonical_result_binding=binding,
        limitations=limits,
        non_inferences=non,
        payload_digest=hashlib.sha256(
            canonical_json_bytes(evidence.to_payload())
        ).hexdigest(),
    )


def _evidence_from_subject(
    evidence: EvidenceItemRevision, expected_subtype: str
) -> Mapping[str, Any]:
    if (
        type(evidence) is not EvidenceItemRevision
        or evidence.subtype != expected_subtype
    ):
        raise ValueError("Evidence is not the requested Admission subtype")
    return deep_thaw(evidence.subject)


def admission_case_from_evidence(evidence: EvidenceItemRevision) -> AdmissionCaseRecord:
    subject = _closed(
        _evidence_from_subject(evidence, ADMISSION_CASE_EVIDENCE_SUBTYPE),
        frozenset(
            {
                "project_id",
                "mission_id",
                "candidate_ref",
                "triage_ref",
                "candidate_disposition",
                "exact_claim",
                "candidate_author_thread_id",
                "triage_reviewer_thread_id",
                "source_closure",
                "canonical_effect",
                "mathematical_effect",
                "mission_effect",
                "strategy_effect",
                "public_effect",
            }
        ),
        "Admission Case subject",
    )
    candidate = CandidateA1Ref.from_payload(subject["candidate_ref"])
    if subject["mission_id"] != candidate.mission_id:
        raise ValueError("Admission Case Mission is stale")
    return AdmissionCaseRecord(
        evidence=evidence,
        project_id=subject["project_id"],
        candidate_ref=candidate,
        triage_ref=AdmissionEvidenceRef.from_payload(subject["triage_ref"]),
        candidate_disposition=subject["candidate_disposition"],
        exact_claim=subject["exact_claim"],
        candidate_author_thread_id=subject["candidate_author_thread_id"],
        triage_reviewer_thread_id=subject["triage_reviewer_thread_id"],
        source_closure=tuple(
            AdmissionOwnerRevisionRef.from_payload(item)
            for item in subject["source_closure"]
        ),
        limitations=evidence.limitations,
        non_inferences=evidence.non_inferences,
        payload_digest=hashlib.sha256(
            canonical_json_bytes(evidence.to_payload())
        ).hexdigest(),
    )


def admission_review_from_evidence(
    evidence: EvidenceItemRevision,
) -> AdmissionReviewRecord:
    subject = _closed(
        _evidence_from_subject(evidence, ADMISSION_REVIEW_EVIDENCE_SUBTYPE),
        frozenset(
            {
                "mission_id",
                "candidate_ref",
                "case_ref",
                "reviewer",
                "disposition",
                "review_finding",
                "objections",
                "cited_basis",
                "canonical_effect",
                "mathematical_effect",
                "mission_effect",
                "strategy_effect",
                "public_effect",
            }
        ),
        "Admission Review subject",
    )
    return AdmissionReviewRecord(
        evidence=evidence,
        candidate_ref=CandidateA1Ref.from_payload(subject["candidate_ref"]),
        case_ref=AdmissionEvidenceRef.from_payload(subject["case_ref"]),
        reviewer=AdmissionActorProvenance.from_payload(subject["reviewer"]),
        disposition=subject["disposition"],
        review_finding=subject["review_finding"],
        objections=tuple(
            AdmissionMaterialObjection.from_payload(item)
            for item in subject["objections"]
        ),
        cited_basis=tuple(
            AdmissionOwnerRevisionRef.from_payload(item)
            for item in subject["cited_basis"]
        ),
        limitations=evidence.limitations,
        non_inferences=evidence.non_inferences,
        payload_digest=hashlib.sha256(
            canonical_json_bytes(evidence.to_payload())
        ).hexdigest(),
    )


def admission_decision_from_evidence(
    evidence: EvidenceItemRevision,
) -> AdmissionDecisionRecord:
    subject = _closed(
        _evidence_from_subject(evidence, ADMISSION_DECISION_EVIDENCE_SUBTYPE),
        frozenset(
            {
                "mission_id",
                "candidate_ref",
                "case_ref",
                "review_ref",
                "admitter",
                "disposition",
                "decision_basis",
                "objections",
                "cited_basis",
                "canonical_result_binding",
                "canonical_effect",
                "mathematical_effect",
                "mission_effect",
                "strategy_effect",
                "public_effect",
            }
        ),
        "Admission Decision subject",
    )
    return AdmissionDecisionRecord(
        evidence=evidence,
        candidate_ref=CandidateA1Ref.from_payload(subject["candidate_ref"]),
        case_ref=AdmissionEvidenceRef.from_payload(subject["case_ref"]),
        review_ref=AdmissionEvidenceRef.from_payload(subject["review_ref"]),
        admitter=AdmissionActorProvenance.from_payload(subject["admitter"]),
        disposition=subject["disposition"],
        decision_basis=subject["decision_basis"],
        objections=tuple(
            AdmissionMaterialObjection.from_payload(item)
            for item in subject["objections"]
        ),
        cited_basis=tuple(
            AdmissionOwnerRevisionRef.from_payload(item)
            for item in subject["cited_basis"]
        ),
        canonical_result_binding=subject["canonical_result_binding"],
        limitations=evidence.limitations,
        non_inferences=evidence.non_inferences,
        payload_digest=hashlib.sha256(
            canonical_json_bytes(evidence.to_payload())
        ).hexdigest(),
    )


ADMISSION_RESERVED_SUBTYPES = frozenset(
    {
        ADMISSION_CASE_EVIDENCE_SUBTYPE,
        ADMISSION_REVIEW_EVIDENCE_SUBTYPE,
        ADMISSION_DECISION_EVIDENCE_SUBTYPE,
    }
)
ADMISSION_RESERVED_PREFIXES = (
    ADMISSION_CASE_EVIDENCE_ID_PREFIX,
    ADMISSION_REVIEW_EVIDENCE_ID_PREFIX,
    ADMISSION_DECISION_EVIDENCE_ID_PREFIX,
)


__all__ = [
    name
    for name in globals()
    if name.startswith("ADMISSION_")
    or name.startswith("Admission")
    or name.startswith("admission_")
    or name.startswith("prepare_admission_")
]
