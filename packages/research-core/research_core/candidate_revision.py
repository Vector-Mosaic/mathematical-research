"""Direct, proof-neutral Candidate revisions for the successor RH workspace.

Candidate values preserve mathematical proposals, their exact standing, their
genealogy, and the owner revisions on which they depend.  They never change
canonical mathematics.  Preparation and commit both require the active direct
Executive Epoch authority; the value itself is protected by deterministic
validation and content identity rather than a process-local token or HMAC.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .json_support import canonical_json_bytes, loads_strict_json_object
from .mission_executive import (
    DirectExecutiveEpochAuthority,
    reissue_direct_executive_epoch_authority,
)
from .research_model import OperationFailure, OperationResult, deep_freeze, deep_thaw
from .validator import _schema_violations


CANDIDATE_REVISION_SCHEMA_VERSION = 2
CANDIDATE_REVISION_SCHEMA_REPO_PATH = (
    "contracts/schemas/candidate_revision.v2.schema.json"
)
CANDIDATE_REVISION_V3_SCHEMA_VERSION = 3
CANDIDATE_REVISION_V3_SCHEMA_REPO_PATH = (
    "contracts/schemas/candidate_revision.v3.schema.json"
)
CANDIDATE_REVISION_V4_SCHEMA_VERSION = 4
CANDIDATE_REVISION_V4_SCHEMA_REPO_PATH = (
    "contracts/schemas/candidate_revision.v4.schema.json"
)

_IDENTITY_FIELDS = (
    "proposal_kind",
    "exact_statement",
    "mechanism",
    "scope_and_reach",
    "hypotheses",
    "domain",
    "normalization",
)
_UNORDERED_TEXT_FIELDS = (
    "objects",
    "hypotheses",
    "quantifiers",
    "normalization",
    "obligations",
    "gaps",
    "objections",
    "circularity_risks",
    "falsifiers",
    "discriminators",
    "limitations",
    "non_inferences",
)
_A1_REQUIREMENT = {
    "kind": "candidate_a1_requirement",
    "classification": "purported_complete_rh_proof_or_disproof",
    "required_atomic_effect": "open_preservation_first_a1_hold",
}
_RH_DIRECT_ASSERTION_RE = re.compile(
    r"(?:\b(?:prove[sd]?|disprove[sd]?|refute[sd]?|resolve[sd]?|"
    r"establish(?:es|ed)?)\b.{0,80}\b(?:rh|riemann hypothesis)\b)"
    r"|(?:\b(?:rh|riemann hypothesis)\b.{0,80}\b(?:holds|is (?:true|false|"
    r"valid)|has been (?:proved|disproved|refuted|resolved|established))\b)"
)
_RH_COMPLETE_SCOPE_RE = re.compile(
    r"\b(?:complete|full|global|unconditional)\b.{0,80}"
    r"\b(?:proof|disproof|resolution|theorem|rh|riemann hypothesis)\b"
    r"|\b(?:proof|disproof|resolution|theorem|rh|riemann hypothesis)\b"
    r".{0,80}\b(?:complete|full|global|unconditional)\b"
)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _failure(code: str, location: str, message: str) -> OperationResult[Any]:
    return OperationResult(
        value=None,
        failure=OperationFailure(code, location, message),
    )


def _default_repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _schema_for_version(
    schema_version: int,
    repo_root: Path | str | None,
) -> tuple[Mapping[str, Any] | None, str | None, OperationResult[Any] | None]:
    root = _default_repo_root() if repo_root is None else Path(repo_root).resolve()
    schema_paths = {
        CANDIDATE_REVISION_SCHEMA_VERSION: CANDIDATE_REVISION_SCHEMA_REPO_PATH,
        CANDIDATE_REVISION_V3_SCHEMA_VERSION: CANDIDATE_REVISION_V3_SCHEMA_REPO_PATH,
        CANDIDATE_REVISION_V4_SCHEMA_VERSION: CANDIDATE_REVISION_V4_SCHEMA_REPO_PATH,
    }
    schema_path = schema_paths.get(schema_version)
    if schema_path is None:
        return (
            None,
            None,
            _failure(
                "candidate_contract_schema_invalid",
                "candidate_revision.schema_version",
                f"unsupported Candidate schema version {schema_version!r}",
            ),
        )
    path = (root / schema_path).resolve()
    try:
        path.relative_to(root)
        raw = path.read_bytes()
        schema = loads_strict_json_object(raw)
    except (OSError, UnicodeDecodeError, TypeError, ValueError) as exc:
        return (
            None,
            None,
            _failure(
                "candidate_contract_schema_invalid",
                str(path),
                str(exc),
            ),
        )
    return schema, _sha256(raw), None


def _schema(
    repo_root: Path | str | None,
) -> tuple[Mapping[str, Any] | None, str | None, OperationResult[Any] | None]:
    """Load immutable Candidate-v2 schema bytes for compatibility callers."""

    return _schema_for_version(CANDIDATE_REVISION_SCHEMA_VERSION, repo_root)


def candidate_schema_digest(repo_root: Path | str | None = None) -> str:
    """Return the digest of the exact successor Candidate schema bytes."""

    _, digest, failed = _schema(repo_root)
    if failed is not None or digest is None:
        assert failed is not None and failed.failure is not None
        raise ValueError(failed.failure.message)
    return digest


def candidate_schema_digest_v3(repo_root: Path | str | None = None) -> str:
    """Return the digest of the exact Candidate-v3 schema bytes."""

    _, digest, failed = _schema_for_version(
        CANDIDATE_REVISION_V3_SCHEMA_VERSION, repo_root
    )
    if failed is not None or digest is None:
        assert failed is not None and failed.failure is not None
        raise ValueError(failed.failure.message)
    return digest


def candidate_schema_digest_v4(repo_root: Path | str | None = None) -> str:
    """Return the digest of the exact Candidate-v4 schema bytes."""

    _, digest, failed = _schema_for_version(
        CANDIDATE_REVISION_V4_SCHEMA_VERSION, repo_root
    )
    if failed is not None or digest is None:
        assert failed is not None and failed.failure is not None
        raise ValueError(failed.failure.message)
    return digest


def _require_text(value: object, location: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{location} must be a nonempty string")
    return value


def _require_positive_int(value: object, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{location} must be a positive integer")
    return value


def _require_sha256(value: object, location: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{64}", value) is None:
        raise ValueError(f"{location} must be lowercase SHA-256")
    return value


def _normalize_texts(
    values: Sequence[str] | None,
    location: str,
) -> list[str]:
    if values is None:
        return []
    if isinstance(values, (str, bytes, bytearray)):
        raise ValueError(f"{location} must be an array")
    normalized = [_require_text(value, location) for value in values]
    if len(normalized) != len(set(normalized)):
        raise ValueError(f"{location} must not contain duplicates")
    return sorted(normalized)


def _normalize_mappings(
    values: Sequence[Mapping[str, Any]] | None,
    location: str,
) -> list[Mapping[str, Any]]:
    if values is None:
        return []
    if isinstance(values, (str, bytes, bytearray)):
        raise ValueError(f"{location} must be an array")
    normalized: list[Mapping[str, Any]] = []
    for value in values:
        if not isinstance(value, Mapping):
            raise ValueError(f"{location} entries must be objects")
        item = deep_thaw(value)
        selection = item.get("selection")
        if isinstance(selection, Mapping) and "treatment_ids" in selection:
            selection["treatment_ids"] = _normalize_texts(
                selection["treatment_ids"], f"{location}.selection.treatment_ids"
            )
        normalized.append(item)
    keys = [canonical_json_bytes(value) for value in normalized]
    if len(keys) != len(set(keys)):
        raise ValueError(f"{location} must not contain duplicates")
    return sorted(normalized, key=canonical_json_bytes)


def _normalize_argument_edges(
    values: Sequence[Mapping[str, Any]] | None,
) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    for index, value in enumerate(_normalize_mappings(values, "argument_edges")):
        edge = deep_thaw(value)
        if "premises" in edge:
            edge["premises"] = _normalize_texts(
                edge["premises"],
                f"argument_edges[{index}].premises",
            )
        authority = edge.get("authority")
        if isinstance(authority, Mapping) and "refs" in authority:
            normalized_authority = deep_thaw(authority)
            normalized_authority["refs"] = _normalize_mappings(
                normalized_authority["refs"],
                f"argument_edges[{index}].authority.refs",
            )
            edge["authority"] = normalized_authority
        result.append(edge)
    keys = [canonical_json_bytes(value) for value in result]
    if len(keys) != len(set(keys)):
        raise ValueError("argument_edges must not contain duplicates")
    return sorted(result, key=canonical_json_bytes)


def _reference_rows(
    document: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...]:
    rows: list[Mapping[str, Any]] = [
        deep_thaw(value) for value in document.get("supporting_refs", ())
    ]
    for edge in document.get("argument_edges", ()):
        rows.extend(deep_thaw(value) for value in edge["authority"].get("refs", ()))
    for edge in document.get("genealogy", ()):
        reference = edge["candidate_ref"]
        rows.append(
            {
                "kind": "candidate",
                "id": reference["candidate_id"],
                "revision": reference["revision"],
                "digest_sha256": reference["digest_sha256"],
            }
        )
    # Treatment selection qualifies mathematical use, not the exact owner head
    # used by retention/concurrency. Do not make two selections two heads.
    exact_rows = [
        {key: value[key] for key in ("kind", "id", "revision", "digest_sha256")}
        for value in rows
    ]
    unique = {canonical_json_bytes(value): value for value in exact_rows}
    return tuple(deep_freeze(unique[key]) for key in sorted(unique))


def _candidate_v2_dependency_heads(
    document: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...]:
    """Return the exact owner revision references bound by a Candidate."""

    return _reference_rows(document)


def _validate_candidate_revision(
    document: Mapping[str, Any],
    *,
    schema_version: int,
    repo_root: Path | str | None = None,
) -> OperationResult[Mapping[str, Any]]:
    """Validate one versioned sparse executive-authored Candidate meaning."""

    if not isinstance(document, Mapping):
        return _failure(
            "candidate_contract_invalid",
            "candidate_revision",
            "must be an object",
        )
    schema, schema_digest, failed = _schema_for_version(schema_version, repo_root)
    if failed is not None or schema is None or schema_digest is None:
        return failed or _failure(
            "candidate_contract_schema_invalid",
            "candidate_revision",
            "schema could not be loaded",
        )
    violations = _schema_violations(document, schema, schema, "$")
    if violations:
        location, message = violations[0]
        return _failure(
            "candidate_contract_invalid",
            f"candidate_revision.{location}",
            message,
        )
    if document["contract_schema_sha256"] != schema_digest:
        return _failure(
            "candidate_contract_schema_stale",
            "candidate_revision.contract_schema_sha256",
            "does not bind the current Candidate schema bytes",
        )

    for field_name in _UNORDERED_TEXT_FIELDS:
        if field_name in document and list(document[field_name]) != sorted(
            document[field_name]
        ):
            return _failure(
                "candidate_noncanonical_order",
                f"candidate_revision.{field_name}",
                "unordered semantic values must use canonical order",
            )
    for field_name in ("supporting_refs", "genealogy", "argument_edges"):
        if field_name not in document:
            continue
        values = [deep_thaw(value) for value in document[field_name]]
        if values != sorted(values, key=canonical_json_bytes):
            return _failure(
                "candidate_noncanonical_order",
                f"candidate_revision.{field_name}",
                "unordered semantic references must use canonical order",
            )

    edge_ids: set[str] = set()
    for index, edge in enumerate(document.get("argument_edges", ())):
        if list(edge["premises"]) != sorted(edge["premises"]):
            return _failure(
                "candidate_noncanonical_order",
                f"candidate_revision.argument_edges[{index}].premises",
                "premises must use canonical order",
            )
        edge_id = str(edge["edge_id"])
        if edge_id in edge_ids:
            return _failure(
                "candidate_edge_collision",
                f"candidate_revision.argument_edges[{index}].edge_id",
                "duplicates an existing Candidate edge identity",
            )
        edge_ids.add(edge_id)
        authority_refs = list(edge["authority"].get("refs", ()))
        if authority_refs != sorted(authority_refs, key=canonical_json_bytes):
            return _failure(
                "candidate_noncanonical_order",
                f"candidate_revision.argument_edges[{index}].authority.refs",
                "edge authority references must use canonical order",
            )

    references_by_owner: dict[tuple[str, str], tuple[int, str]] = {}
    authored_references = list(document.get("supporting_refs", ()))
    for edge in document.get("argument_edges", ()):
        authored_references.extend(edge["authority"].get("refs", ()))
    for index, reference in enumerate(authored_references):
        selection = reference.get("selection")
        if isinstance(selection, Mapping) and selection.get("mode") == "treatments":
            if list(selection["treatment_ids"]) != sorted(selection["treatment_ids"]):
                return _failure(
                    "candidate_noncanonical_order",
                    f"candidate_revision.references[{index}].selection.treatment_ids",
                    "selected treatment identities must use canonical order",
                )
    for index, reference in enumerate(_reference_rows(document)):
        if (
            reference["kind"] == "candidate"
            and reference["id"] == document["candidate_id"]
        ):
            return _failure(
                "candidate_self_reference",
                f"candidate_revision.references[{index}]",
                "a Candidate revision must not depend on its own prior payload",
            )
        owner = (str(reference["kind"]), str(reference["id"]))
        version = (int(reference["revision"]), str(reference["digest_sha256"]))
        existing = references_by_owner.get(owner)
        if existing is not None and existing != version:
            return _failure(
                "candidate_reference_collision",
                f"candidate_revision.references[{index}]",
                "one owner identity is bound to multiple revisions or digests",
            )
        references_by_owner[owner] = version

    if (
        document["proposal_kind"]
        in {
            "mechanism",
            "construction",
            "proof_architecture",
        }
        and "mechanism" not in document
    ):
        return _failure(
            "candidate_mechanism_missing",
            "candidate_revision.mechanism",
            "this Candidate kind requires its exact mechanism or architecture",
        )
    return OperationResult(value=deep_freeze(deep_thaw(document)))


def validate_candidate_revision_v2(
    document: Mapping[str, Any],
    *,
    repo_root: Path | str | None = None,
) -> OperationResult[Mapping[str, Any]]:
    """Validate immutable Candidate-v2 meaning and its legacy contract."""

    return _validate_candidate_revision(
        document,
        schema_version=CANDIDATE_REVISION_SCHEMA_VERSION,
        repo_root=repo_root,
    )


def validate_candidate_revision_v3(
    document: Mapping[str, Any],
    *,
    repo_root: Path | str | None = None,
) -> OperationResult[Mapping[str, Any]]:
    """Validate Candidate-v3 meaning and its structured complete-target claim."""

    return _validate_candidate_revision(
        document,
        schema_version=CANDIDATE_REVISION_V3_SCHEMA_VERSION,
        repo_root=repo_root,
    )


def validate_candidate_revision_v4(
    document: Mapping[str, Any],
    *,
    repo_root: Path | str | None = None,
) -> OperationResult[Mapping[str, Any]]:
    """Validate Candidate-v4 exact selected Context use and complete-target reach."""

    return _validate_candidate_revision(
        document,
        schema_version=CANDIDATE_REVISION_V4_SCHEMA_VERSION,
        repo_root=repo_root,
    )


def validate_candidate_revision(
    document: Mapping[str, Any],
    *,
    repo_root: Path | str | None = None,
) -> OperationResult[Mapping[str, Any]]:
    """Validate one supported Candidate revision by its immutable schema version."""

    if not isinstance(document, Mapping):
        return _failure(
            "candidate_contract_invalid",
            "candidate_revision",
            "must be an object",
        )
    schema_version = document.get("schema_version")
    if schema_version == CANDIDATE_REVISION_SCHEMA_VERSION:
        return validate_candidate_revision_v2(document, repo_root=repo_root)
    if schema_version == CANDIDATE_REVISION_V3_SCHEMA_VERSION:
        return validate_candidate_revision_v3(document, repo_root=repo_root)
    if schema_version == CANDIDATE_REVISION_V4_SCHEMA_VERSION:
        return validate_candidate_revision_v4(document, repo_root=repo_root)
    return _failure(
        "candidate_contract_invalid",
        "candidate_revision.schema_version",
        f"unsupported Candidate schema version {schema_version!r}",
    )


@dataclass(frozen=True, slots=True)
class CandidateRevisionV2:
    """One sparse, deterministic, executive-authored Candidate meaning."""

    document: Mapping[str, Any]
    digest_sha256: str
    dependency_heads: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "document", deep_freeze(self.document))
        object.__setattr__(
            self,
            "dependency_heads",
            tuple(deep_freeze(value) for value in self.dependency_heads),
        )
        validated = validate_candidate_revision_v2(self.document)
        if not validated.ok:
            assert validated.failure is not None
            raise ValueError(validated.failure.message)
        if self.digest_sha256 != _sha256(canonical_json_bytes(self.document)):
            raise ValueError("Candidate revision digest is stale")
        if self.dependency_heads != _candidate_v2_dependency_heads(self.document):
            raise ValueError("Candidate dependency heads are stale")

    def verify_integrity(self) -> None:
        if type(self) is not CandidateRevisionV2:
            raise ValueError("Candidate subclasses have no authority")
        self.__post_init__()

    def verify_issued(self) -> None:
        """Compatibility spelling for callers while the old owner is deleted."""

        self.verify_integrity()


@dataclass(frozen=True, slots=True)
class PersistedCandidateRevisionV2:
    """One Store-owned Candidate revision and its head metadata."""

    record: CandidateRevisionV2
    revision: int
    payload_digest: str
    predecessor_revision: int | None
    created_actor: str
    created_at: str
    is_current_head: bool

    def __post_init__(self) -> None:
        if type(self.record) is not CandidateRevisionV2:
            raise TypeError("persisted Candidate requires its exact value type")
        self.record.verify_integrity()
        _require_positive_int(self.revision, "Candidate revision")
        if self.predecessor_revision is None:
            if self.revision != 1:
                raise ValueError(
                    "only the first Candidate revision lacks a predecessor"
                )
        else:
            _require_positive_int(
                self.predecessor_revision,
                "Candidate predecessor revision",
            )
            if self.predecessor_revision != self.revision - 1:
                raise ValueError("Candidate predecessor revision is not contiguous")
        if (
            _require_sha256(self.payload_digest, "Candidate payload digest")
            != self.record.digest_sha256
        ):
            raise ValueError("persisted Candidate payload digest is stale")
        _require_text(self.created_actor, "Candidate created actor")
        _require_text(self.created_at, "Candidate created at")
        if not isinstance(self.is_current_head, bool):
            raise TypeError("Candidate current-head state must be boolean")


@dataclass(frozen=True, slots=True)
class CandidateRevisionV3:
    """One sparse Candidate with optional structured complete-target reach."""

    document: Mapping[str, Any]
    digest_sha256: str
    dependency_heads: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "document", deep_freeze(self.document))
        object.__setattr__(
            self,
            "dependency_heads",
            tuple(deep_freeze(value) for value in self.dependency_heads),
        )
        validated = validate_candidate_revision_v3(self.document)
        if not validated.ok:
            assert validated.failure is not None
            raise ValueError(validated.failure.message)
        if self.digest_sha256 != _sha256(canonical_json_bytes(self.document)):
            raise ValueError("Candidate revision digest is stale")
        if self.dependency_heads != _candidate_v2_dependency_heads(self.document):
            raise ValueError("Candidate dependency heads are stale")

    def verify_integrity(self) -> None:
        if type(self) is not CandidateRevisionV3:
            raise ValueError("Candidate subclasses have no authority")
        self.__post_init__()

    def verify_issued(self) -> None:
        self.verify_integrity()


@dataclass(frozen=True, slots=True)
class PersistedCandidateRevisionV3:
    """One Store-owned Candidate-v3 revision and its head metadata."""

    record: CandidateRevisionV3
    revision: int
    payload_digest: str
    predecessor_revision: int | None
    created_actor: str
    created_at: str
    is_current_head: bool

    def __post_init__(self) -> None:
        if type(self.record) is not CandidateRevisionV3:
            raise TypeError("persisted Candidate requires its exact value type")
        self.record.verify_integrity()
        _require_positive_int(self.revision, "Candidate revision")
        if self.predecessor_revision is None:
            if self.revision != 1:
                raise ValueError(
                    "only the first Candidate revision lacks a predecessor"
                )
        else:
            _require_positive_int(
                self.predecessor_revision,
                "Candidate predecessor revision",
            )
            if self.predecessor_revision != self.revision - 1:
                raise ValueError("Candidate predecessor revision is not contiguous")
        if (
            _require_sha256(self.payload_digest, "Candidate payload digest")
            != self.record.digest_sha256
        ):
            raise ValueError("persisted Candidate payload digest is stale")
        _require_text(self.created_actor, "Candidate created actor")
        _require_text(self.created_at, "Candidate created at")
        if not isinstance(self.is_current_head, bool):
            raise TypeError("Candidate current-head state must be boolean")


@dataclass(frozen=True, slots=True)
class CandidateRevisionV4:
    """One sparse Candidate with exact Context treatment reliance and complete-target reach."""

    document: Mapping[str, Any]
    digest_sha256: str
    dependency_heads: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "document", deep_freeze(self.document))
        object.__setattr__(
            self,
            "dependency_heads",
            tuple(deep_freeze(value) for value in self.dependency_heads),
        )
        validated = validate_candidate_revision_v4(self.document)
        if not validated.ok:
            assert validated.failure is not None
            raise ValueError(validated.failure.message)
        if self.digest_sha256 != _sha256(canonical_json_bytes(self.document)):
            raise ValueError("Candidate revision digest is stale")
        if self.dependency_heads != _candidate_v2_dependency_heads(self.document):
            raise ValueError("Candidate dependency heads are stale")

    def verify_integrity(self) -> None:
        if type(self) is not CandidateRevisionV4:
            raise ValueError("Candidate subclasses have no authority")
        self.__post_init__()

    def verify_issued(self) -> None:
        self.verify_integrity()


@dataclass(frozen=True, slots=True)
class PersistedCandidateRevisionV4:
    """One Store-owned Candidate-v4 revision and its head metadata."""

    record: CandidateRevisionV4
    revision: int
    payload_digest: str
    predecessor_revision: int | None
    created_actor: str
    created_at: str
    is_current_head: bool

    def __post_init__(self) -> None:
        if type(self.record) is not CandidateRevisionV4:
            raise TypeError("persisted Candidate requires its exact value type")
        self.record.verify_integrity()
        _require_positive_int(self.revision, "Candidate revision")
        if self.predecessor_revision is None:
            if self.revision != 1:
                raise ValueError(
                    "only the first Candidate revision lacks a predecessor"
                )
        else:
            _require_positive_int(
                self.predecessor_revision,
                "Candidate predecessor revision",
            )
            if self.predecessor_revision != self.revision - 1:
                raise ValueError("Candidate predecessor revision is not contiguous")
        if (
            _require_sha256(self.payload_digest, "Candidate payload digest")
            != self.record.digest_sha256
        ):
            raise ValueError("persisted Candidate payload digest is stale")
        _require_text(self.created_actor, "Candidate created actor")
        _require_text(self.created_at, "Candidate created at")
        if not isinstance(self.is_current_head, bool):
            raise TypeError("Candidate current-head state must be boolean")


CandidateRevision = CandidateRevisionV2 | CandidateRevisionV3 | CandidateRevisionV4
PersistedCandidateRevision = (
    PersistedCandidateRevisionV2 | PersistedCandidateRevisionV3 | PersistedCandidateRevisionV4
)


def _candidate_revision_from_document(
    document: Mapping[str, Any],
    *,
    repo_root: Path | str | None = None,
) -> CandidateRevision:
    validated = validate_candidate_revision(document, repo_root=repo_root)
    if not validated.ok or validated.value is None:
        assert validated.failure is not None
        raise ValueError(validated.failure.message)
    material = deep_thaw(validated.value)
    values = {
        "document": material,
        "digest_sha256": _sha256(canonical_json_bytes(material)),
        "dependency_heads": _candidate_v2_dependency_heads(material),
    }
    if material["schema_version"] == CANDIDATE_REVISION_SCHEMA_VERSION:
        return CandidateRevisionV2(**values)
    if material["schema_version"] == CANDIDATE_REVISION_V3_SCHEMA_VERSION:
        return CandidateRevisionV3(**values)
    return CandidateRevisionV4(**values)


def _identity_material(document: Mapping[str, Any]) -> Mapping[str, Any]:
    return deep_freeze(
        {
            key: deep_thaw(document[key]) if key in document else None
            for key in _IDENTITY_FIELDS
        }
    )


def _persisted_reference(
    value: PersistedCandidateRevision,
) -> Mapping[str, Any]:
    return deep_freeze(
        {
            "candidate_id": value.record.document["candidate_id"],
            "revision": value.revision,
            "digest_sha256": value.payload_digest,
        }
    )


def _prepare_candidate_revision_v2_legacy_document(
    *,
    authority: DirectExecutiveEpochAuthority,
    candidate_id: str,
    proposal_kind: str,
    exact_statement: str,
    standing: Mapping[str, Any],
    predecessor: PersistedCandidateRevisionV2 | None = None,
    mechanism: str | None = None,
    scope_and_reach: str | None = None,
    objects: Sequence[str] | None = None,
    hypotheses: Sequence[str] | None = None,
    domain: str | None = None,
    quantifiers: Sequence[str] | None = None,
    normalization: Sequence[str] | None = None,
    argument_edges: Sequence[Mapping[str, Any]] | None = None,
    supporting_refs: Sequence[Mapping[str, Any]] | None = None,
    obligations: Sequence[str] | None = None,
    gaps: Sequence[str] | None = None,
    objections: Sequence[str] | None = None,
    circularity_risks: Sequence[str] | None = None,
    falsifiers: Sequence[str] | None = None,
    discriminators: Sequence[str] | None = None,
    limitations: Sequence[str] | None = None,
    non_inferences: Sequence[str] | None = None,
    genealogy: Sequence[Mapping[str, Any]] | None = None,
    provenance: Mapping[str, Any] | None = None,
    repo_root: Path | str | None = None,
) -> CandidateRevisionV2:
    """Prepare immutable v2 material for historical compatibility fixtures."""

    if type(authority) is not DirectExecutiveEpochAuthority:
        raise TypeError("Candidate requires direct Executive Epoch authority")
    authority.verify_integrity()
    candidate_id = _require_text(candidate_id, "candidate_id")
    proposal_kind = _require_text(proposal_kind, "proposal_kind")
    exact_statement = _require_text(exact_statement, "exact_statement")
    if not isinstance(standing, Mapping):
        raise ValueError("standing must be an object")
    if predecessor is not None:
        if type(predecessor) is not PersistedCandidateRevisionV2:
            raise TypeError("predecessor must be a persisted Candidate revision")
        predecessor.__post_init__()
        if not predecessor.is_current_head:
            raise ValueError("Candidate predecessor must be the current head")
        if predecessor.record.document["mission_id"] != authority.mission_id:
            raise ValueError("Candidate predecessor belongs to another Mission")

    if provenance is None:
        if (
            predecessor is not None
            and predecessor.record.document["candidate_id"] == candidate_id
        ):
            selected_provenance = deep_thaw(predecessor.record.document["provenance"])
        else:
            selected_provenance = {
                "kind": "native_executive_epoch",
                "executive_epoch_id": authority.executive_epoch_id,
                "executive_epoch_authority_sha256": authority.authority_sha256,
            }
    else:
        if not isinstance(provenance, Mapping):
            raise ValueError("provenance must be an object")
        selected_provenance = deep_thaw(provenance)

    document: dict[str, Any] = {
        "schema_version": CANDIDATE_REVISION_SCHEMA_VERSION,
        "kind": "candidate_revision",
        "contract_schema_sha256": candidate_schema_digest(repo_root),
        "candidate_id": candidate_id,
        "mission_id": _require_text(authority.mission_id, "mission_id"),
        "proposal_kind": proposal_kind,
        "exact_statement": exact_statement,
        "standing": deep_thaw(standing),
        "provenance": selected_provenance,
        "authority_class": "candidate_only",
        "canonical_effect": "none",
    }
    for field_name, value in (
        ("mechanism", mechanism),
        ("scope_and_reach", scope_and_reach),
        ("domain", domain),
    ):
        if value is not None:
            document[field_name] = _require_text(value, field_name)
    for field_name, values in (
        ("objects", objects),
        ("hypotheses", hypotheses),
        ("quantifiers", quantifiers),
        ("normalization", normalization),
        ("obligations", obligations),
        ("gaps", gaps),
        ("objections", objections),
        ("circularity_risks", circularity_risks),
        ("falsifiers", falsifiers),
        ("discriminators", discriminators),
        ("limitations", limitations),
        ("non_inferences", non_inferences),
    ):
        normalized = _normalize_texts(values, field_name)
        if normalized:
            document[field_name] = normalized
    normalized_edges = _normalize_argument_edges(argument_edges)
    if normalized_edges:
        document["argument_edges"] = normalized_edges
    normalized_refs = _normalize_mappings(supporting_refs, "supporting_refs")
    if normalized_refs:
        document["supporting_refs"] = normalized_refs
    normalized_genealogy = _normalize_mappings(genealogy, "genealogy")
    if normalized_genealogy:
        document["genealogy"] = normalized_genealogy

    candidate = _candidate_revision_from_document(document, repo_root=repo_root)
    if type(candidate) is not CandidateRevisionV2:
        raise ValueError("Candidate-v2 preparation produced the wrong schema version")
    if predecessor is None:
        return candidate
    predecessor_id = str(predecessor.record.document["candidate_id"])
    if predecessor_id == candidate_id:
        if _identity_material(candidate.document) != _identity_material(
            predecessor.record.document
        ):
            raise ValueError(
                "identity-defining Candidate change requires a linked new identity"
            )
        return candidate

    expected_link = _persisted_reference(predecessor)
    linked = any(
        deep_thaw(edge["candidate_ref"]) == deep_thaw(expected_link)
        for edge in candidate.document.get("genealogy", ())
    )
    if not linked:
        raise ValueError(
            "new Candidate identity requires an exact genealogy link to its predecessor"
        )
    return candidate


def prepare_candidate_revision_v3(
    *,
    authority: DirectExecutiveEpochAuthority,
    candidate_id: str,
    proposal_kind: str,
    exact_statement: str,
    standing: Mapping[str, Any],
    predecessor: PersistedCandidateRevision | None = None,
    complete_target_claim: Mapping[str, Any] | None = None,
    mechanism: str | None = None,
    scope_and_reach: str | None = None,
    objects: Sequence[str] | None = None,
    hypotheses: Sequence[str] | None = None,
    domain: str | None = None,
    quantifiers: Sequence[str] | None = None,
    normalization: Sequence[str] | None = None,
    argument_edges: Sequence[Mapping[str, Any]] | None = None,
    supporting_refs: Sequence[Mapping[str, Any]] | None = None,
    obligations: Sequence[str] | None = None,
    gaps: Sequence[str] | None = None,
    objections: Sequence[str] | None = None,
    circularity_risks: Sequence[str] | None = None,
    falsifiers: Sequence[str] | None = None,
    discriminators: Sequence[str] | None = None,
    limitations: Sequence[str] | None = None,
    non_inferences: Sequence[str] | None = None,
    genealogy: Sequence[Mapping[str, Any]] | None = None,
    provenance: Mapping[str, Any] | None = None,
    repo_root: Path | str | None = None,
) -> CandidateRevisionV3:
    """Prepare one Candidate-v3 revision without fabricating formal ancestry."""

    if type(authority) is not DirectExecutiveEpochAuthority:
        raise TypeError("Candidate requires direct Executive Epoch authority")
    authority.verify_integrity()
    candidate_id = _require_text(candidate_id, "candidate_id")
    proposal_kind = _require_text(proposal_kind, "proposal_kind")
    exact_statement = _require_text(exact_statement, "exact_statement")
    if not isinstance(standing, Mapping):
        raise ValueError("standing must be an object")
    if predecessor is not None:
        if type(predecessor) not in (
            PersistedCandidateRevisionV2,
            PersistedCandidateRevisionV3,
        ):
            raise TypeError("predecessor must be a persisted Candidate revision")
        predecessor.__post_init__()
        if not predecessor.is_current_head:
            raise ValueError("Candidate predecessor must be the current head")
        if predecessor.record.document["mission_id"] != authority.mission_id:
            raise ValueError("Candidate predecessor belongs to another Mission")

    if provenance is None:
        if (
            predecessor is not None
            and predecessor.record.document["candidate_id"] == candidate_id
        ):
            selected_provenance = deep_thaw(predecessor.record.document["provenance"])
        else:
            selected_provenance = {
                "kind": "native_executive_epoch",
                "executive_epoch_id": authority.executive_epoch_id,
                "executive_epoch_authority_sha256": authority.authority_sha256,
            }
    else:
        if not isinstance(provenance, Mapping):
            raise ValueError("provenance must be an object")
        selected_provenance = deep_thaw(provenance)

    document: dict[str, Any] = {
        "schema_version": CANDIDATE_REVISION_V3_SCHEMA_VERSION,
        "kind": "candidate_revision",
        "contract_schema_sha256": candidate_schema_digest_v3(repo_root),
        "candidate_id": candidate_id,
        "mission_id": _require_text(authority.mission_id, "mission_id"),
        "proposal_kind": proposal_kind,
        "exact_statement": exact_statement,
        "standing": deep_thaw(standing),
        "provenance": selected_provenance,
        "authority_class": "candidate_only",
        "canonical_effect": "none",
    }
    if complete_target_claim is not None:
        if not isinstance(complete_target_claim, Mapping):
            raise ValueError("complete_target_claim must be an object")
        document["complete_target_claim"] = deep_thaw(complete_target_claim)
    for field_name, value in (
        ("mechanism", mechanism),
        ("scope_and_reach", scope_and_reach),
        ("domain", domain),
    ):
        if value is not None:
            document[field_name] = _require_text(value, field_name)
    for field_name, values in (
        ("objects", objects),
        ("hypotheses", hypotheses),
        ("quantifiers", quantifiers),
        ("normalization", normalization),
        ("obligations", obligations),
        ("gaps", gaps),
        ("objections", objections),
        ("circularity_risks", circularity_risks),
        ("falsifiers", falsifiers),
        ("discriminators", discriminators),
        ("limitations", limitations),
        ("non_inferences", non_inferences),
    ):
        normalized = _normalize_texts(values, field_name)
        if normalized:
            document[field_name] = normalized
    normalized_edges = _normalize_argument_edges(argument_edges)
    if normalized_edges:
        document["argument_edges"] = normalized_edges
    normalized_refs = _normalize_mappings(supporting_refs, "supporting_refs")
    if normalized_refs:
        document["supporting_refs"] = normalized_refs
    normalized_genealogy = _normalize_mappings(genealogy, "genealogy")
    if normalized_genealogy:
        document["genealogy"] = normalized_genealogy

    candidate = _candidate_revision_from_document(document, repo_root=repo_root)
    if type(candidate) is not CandidateRevisionV3:
        raise ValueError("Candidate-v3 preparation produced the wrong schema version")
    if predecessor is None:
        return candidate
    predecessor_id = str(predecessor.record.document["candidate_id"])
    if predecessor_id == candidate_id:
        if _identity_material(candidate.document) != _identity_material(
            predecessor.record.document
        ):
            raise ValueError(
                "identity-defining Candidate change requires a linked new identity"
            )
        return candidate

    expected_link = _persisted_reference(predecessor)
    linked = any(
        deep_thaw(edge["candidate_ref"]) == deep_thaw(expected_link)
        for edge in candidate.document.get("genealogy", ())
    )
    if not linked:
        raise ValueError(
            "new Candidate identity requires an exact genealogy link to its predecessor"
        )
    return candidate


def prepare_candidate_revision_v4(
    *,
    authority: DirectExecutiveEpochAuthority,
    candidate_id: str,
    proposal_kind: str,
    exact_statement: str,
    standing: Mapping[str, Any],
    predecessor: PersistedCandidateRevision | None = None,
    complete_target_claim: Mapping[str, Any] | None = None,
    mechanism: str | None = None,
    scope_and_reach: str | None = None,
    objects: Sequence[str] | None = None,
    hypotheses: Sequence[str] | None = None,
    domain: str | None = None,
    quantifiers: Sequence[str] | None = None,
    normalization: Sequence[str] | None = None,
    argument_edges: Sequence[Mapping[str, Any]] | None = None,
    supporting_refs: Sequence[Mapping[str, Any]] | None = None,
    obligations: Sequence[str] | None = None,
    gaps: Sequence[str] | None = None,
    objections: Sequence[str] | None = None,
    circularity_risks: Sequence[str] | None = None,
    falsifiers: Sequence[str] | None = None,
    discriminators: Sequence[str] | None = None,
    limitations: Sequence[str] | None = None,
    non_inferences: Sequence[str] | None = None,
    genealogy: Sequence[Mapping[str, Any]] | None = None,
    provenance: Mapping[str, Any] | None = None,
    repo_root: Path | str | None = None,
) -> CandidateRevisionV4:
    """Prepare one Candidate-v4 revision without fabricating formal ancestry."""

    if type(authority) is not DirectExecutiveEpochAuthority:
        raise TypeError("Candidate requires direct Executive Epoch authority")
    authority.verify_integrity()
    candidate_id = _require_text(candidate_id, "candidate_id")
    proposal_kind = _require_text(proposal_kind, "proposal_kind")
    exact_statement = _require_text(exact_statement, "exact_statement")
    if not isinstance(standing, Mapping):
        raise ValueError("standing must be an object")
    if predecessor is not None:
        if type(predecessor) not in (
            PersistedCandidateRevisionV2,
            PersistedCandidateRevisionV3,
            PersistedCandidateRevisionV4,
        ):
            raise TypeError("predecessor must be a persisted Candidate revision")
        predecessor.__post_init__()
        if not predecessor.is_current_head:
            raise ValueError("Candidate predecessor must be the current head")
        if predecessor.record.document["mission_id"] != authority.mission_id:
            raise ValueError("Candidate predecessor belongs to another Mission")

    if provenance is None:
        if (
            predecessor is not None
            and predecessor.record.document["candidate_id"] == candidate_id
        ):
            selected_provenance = deep_thaw(predecessor.record.document["provenance"])
        else:
            selected_provenance = {
                "kind": "native_executive_epoch",
                "executive_epoch_id": authority.executive_epoch_id,
                "executive_epoch_authority_sha256": authority.authority_sha256,
            }
    else:
        if not isinstance(provenance, Mapping):
            raise ValueError("provenance must be an object")
        selected_provenance = deep_thaw(provenance)

    document: dict[str, Any] = {
        "schema_version": CANDIDATE_REVISION_V4_SCHEMA_VERSION,
        "kind": "candidate_revision",
        "contract_schema_sha256": candidate_schema_digest_v4(repo_root),
        "candidate_id": candidate_id,
        "mission_id": _require_text(authority.mission_id, "mission_id"),
        "proposal_kind": proposal_kind,
        "exact_statement": exact_statement,
        "standing": deep_thaw(standing),
        "provenance": selected_provenance,
        "authority_class": "candidate_only",
        "canonical_effect": "none",
    }
    if complete_target_claim is not None:
        if not isinstance(complete_target_claim, Mapping):
            raise ValueError("complete_target_claim must be an object")
        document["complete_target_claim"] = deep_thaw(complete_target_claim)
    for field_name, value in (
        ("mechanism", mechanism),
        ("scope_and_reach", scope_and_reach),
        ("domain", domain),
    ):
        if value is not None:
            document[field_name] = _require_text(value, field_name)
    for field_name, values in (
        ("objects", objects),
        ("hypotheses", hypotheses),
        ("quantifiers", quantifiers),
        ("normalization", normalization),
        ("obligations", obligations),
        ("gaps", gaps),
        ("objections", objections),
        ("circularity_risks", circularity_risks),
        ("falsifiers", falsifiers),
        ("discriminators", discriminators),
        ("limitations", limitations),
        ("non_inferences", non_inferences),
    ):
        normalized = _normalize_texts(values, field_name)
        if normalized:
            document[field_name] = normalized
    normalized_edges = _normalize_argument_edges(argument_edges)
    if normalized_edges:
        document["argument_edges"] = normalized_edges
    normalized_refs = _normalize_mappings(supporting_refs, "supporting_refs")
    if normalized_refs:
        document["supporting_refs"] = normalized_refs
    normalized_genealogy = _normalize_mappings(genealogy, "genealogy")
    if normalized_genealogy:
        document["genealogy"] = normalized_genealogy

    candidate = _candidate_revision_from_document(document, repo_root=repo_root)
    if type(candidate) is not CandidateRevisionV4:
        raise ValueError("Candidate-v4 preparation produced the wrong schema version")
    if predecessor is None:
        return candidate
    predecessor_id = str(predecessor.record.document["candidate_id"])
    if predecessor_id == candidate_id:
        if _identity_material(candidate.document) != _identity_material(
            predecessor.record.document
        ):
            raise ValueError(
                "identity-defining Candidate change requires a linked new identity"
            )
        return candidate

    expected_link = _persisted_reference(predecessor)
    linked = any(
        deep_thaw(edge["candidate_ref"]) == deep_thaw(expected_link)
        for edge in candidate.document.get("genealogy", ())
    )
    if not linked:
        raise ValueError(
            "new Candidate identity requires an exact genealogy link to its predecessor"
        )
    return candidate


# ``prepare_candidate_revision`` is the current public constructor.  Candidate
# v2/v3 remain readable through their immutable version-owned constructors;
# ordinary new writes use v4 without changing previously frozen meaning.
prepare_candidate_revision = prepare_candidate_revision_v4


def _candidate_v2_requires_a1(document: Mapping[str, Any]) -> bool:
    """Preserve the immutable Candidate-v2 wording-based A1 classification."""

    statement_parts = [document.get("exact_statement"), document.get("mechanism")]
    statement = " ".join(
        value for value in statement_parts if isinstance(value, str) and value
    )
    scope = document.get("scope_and_reach")
    text = " ".join(statement.casefold().split())
    claimed_scope = " ".join(scope.casefold().split()) if isinstance(scope, str) else ""
    return bool(
        _RH_DIRECT_ASSERTION_RE.search(text)
        or _RH_COMPLETE_SCOPE_RE.search(text)
        or _RH_COMPLETE_SCOPE_RE.search(claimed_scope)
    )


def candidate_requires_a1(document: Mapping[str, Any]) -> bool:
    """Identify A1 by the exact version-owned Candidate contract."""

    if document.get("schema_version") == CANDIDATE_REVISION_SCHEMA_VERSION:
        return _candidate_v2_requires_a1(document)
    if document.get("schema_version") in {
        CANDIDATE_REVISION_V3_SCHEMA_VERSION, CANDIDATE_REVISION_V4_SCHEMA_VERSION
    }:
        return "complete_target_claim" in document
    return False


def candidate_complete_target_claim(
    document: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Project exact v3 reach or the explicitly unspecified v2 legacy direction."""

    if document.get("schema_version") in {
        CANDIDATE_REVISION_V3_SCHEMA_VERSION, CANDIDATE_REVISION_V4_SCHEMA_VERSION
    }:
        claim = document.get("complete_target_claim")
        return None if claim is None else deep_freeze(deep_thaw(claim))
    if (
        document.get("schema_version") == CANDIDATE_REVISION_SCHEMA_VERSION
        and _candidate_v2_requires_a1(document)
    ):
        return deep_freeze(
            {
                "target": "riemann_hypothesis",
                "disposition": "legacy_unspecified",
            }
        )
    return None


def candidate_a1_requirement(
    document: Mapping[str, Any],
) -> Mapping[str, Any] | None:
    """Return the inseparable preservation requirement for a complete RH claim."""

    if not candidate_requires_a1(document):
        return None
    return deep_freeze(_A1_REQUIREMENT)


def _candidate_store_dependency_heads(
    record: CandidateRevision,
) -> Mapping[str, tuple[int, str]]:
    prefixes = {
        "evidence": "evidence",
        "context": "context",
        "branch": "branch",
        "candidate": "candidate",
    }
    dependencies: dict[str, tuple[int, str]] = {}
    for reference in record.dependency_heads:
        prefix = prefixes[str(reference["kind"])]
        dependencies[f"{prefix}:{reference['id']}"] = (
            int(reference["revision"]),
            str(reference["digest_sha256"]),
        )
    return deep_freeze(dict(sorted(dependencies.items())))


def commit_candidate_revision(
    store: Any,
    *,
    authority: DirectExecutiveEpochAuthority,
    record: CandidateRevision,
    lease: Any,
    actor: str,
    expected_head_revision: int | None = None,
    expected_head_payload_digest: str | None = None,
    expected_dependency_heads: Mapping[str, tuple[int, str]] | None = None,
    command_id: str | None = None,
) -> Any:
    """Commit one Candidate and any required A1 hold atomically."""

    if type(record) is not CandidateRevisionV4:
        raise TypeError(
            "current Candidate commits require an exact Candidate-v4 value"
        )
    record.verify_integrity()
    if type(authority) is not DirectExecutiveEpochAuthority:
        raise TypeError("Candidate commit requires direct Executive Epoch authority")
    authority.verify_integrity()
    if record.document["mission_id"] != authority.mission_id:
        raise ValueError("Candidate revision belongs to another Mission")
    event_readbacks = store.read_active_executive_epoch(authority.mission_id)
    if event_readbacks is None:
        raise ValueError("Candidate commit requires one active Executive Epoch")
    metadata = store.read_metadata()
    active_authority = reissue_direct_executive_epoch_authority(
        event_readbacks=event_readbacks,
        project_id=_require_text(metadata["project_id"], "project_id"),
        root_identity=_require_text(metadata["root_identity"], "root_identity"),
        canonical_authority_digest=_require_sha256(
            metadata["canonical_authority_digest"],
            "canonical_authority_digest",
        ),
    )
    if authority.authority_sha256 != active_authority.authority_sha256:
        raise ValueError("Candidate authority differs from the active Store epoch")
    provenance = record.document["provenance"]
    if provenance["kind"] == "native_executive_epoch" and (
        provenance["executive_epoch_id"] != active_authority.executive_epoch_id
        or provenance["executive_epoch_authority_sha256"]
        != active_authority.authority_sha256
    ):
        raise ValueError(
            "Candidate native provenance differs from the active Store epoch binding"
        )
    if (expected_head_revision is None) != (expected_head_payload_digest is None):
        raise ValueError("expected Candidate head revision and digest travel together")
    if expected_head_revision is not None:
        _require_positive_int(
            expected_head_revision, "expected Candidate head revision"
        )
        _require_sha256(
            expected_head_payload_digest,
            "expected Candidate head payload digest",
        )
    selected_dependencies = (
        _candidate_store_dependency_heads(record)
        if expected_dependency_heads is None
        else expected_dependency_heads
    )
    dependency_heads = {
        str(key): (int(value[0]), str(value[1]))
        for key, value in selected_dependencies.items()
    }
    return store.commit_candidate_revision(
        executive_epoch_id=active_authority.executive_epoch_id,
        mission_id=str(record.document["mission_id"]),
        candidate_id=str(record.document["candidate_id"]),
        payload=record.document,
        expected_head_revision=expected_head_revision,
        expected_head_payload_digest=expected_head_payload_digest,
        dependency_heads=dependency_heads,
        lease=lease,
        command_id=(
            command_id
            or f"candidate:{record.document['candidate_id']}:{record.digest_sha256}"
        ),
        actor=_require_text(actor, "Candidate actor"),
        expected_canonical_authority_digest=active_authority.canonical_authority_digest,
        a1_requirement=candidate_a1_requirement(record.document),
    )


def read_candidate_revision(
    store: Any,
    *,
    mission_id: str,
    candidate_id: str,
    revision: int | None = None,
) -> PersistedCandidateRevision:
    """Read one current or historical Candidate owner revision."""

    mission_id = _require_text(mission_id, "mission_id")
    candidate_id = _require_text(candidate_id, "candidate_id")
    if revision is not None:
        _require_positive_int(revision, "Candidate revision")
    stored = store.read_candidate_revision(
        candidate_id=candidate_id,
        mission_id=mission_id,
        revision=revision,
    )
    record = _candidate_revision_from_document(stored["payload"])
    if (
        record.document["candidate_id"] != candidate_id
        or record.document["mission_id"] != mission_id
    ):
        raise ValueError("stored Candidate payload identity differs from its owner key")
    current = (
        stored
        if revision is None
        else store.read_candidate_revision(
            candidate_id=candidate_id,
            mission_id=mission_id,
        )
    )
    values = {
        "record": record,
        "revision": int(stored["revision"]),
        "payload_digest": str(stored["payload_digest"]),
        "predecessor_revision": (
            None
            if stored["predecessor_revision"] is None
            else int(stored["predecessor_revision"])
        ),
        "created_actor": str(stored["created_actor"]),
        "created_at": str(stored["created_at"]),
        "is_current_head": (
            int(current["revision"]) == int(stored["revision"])
            and str(current["payload_digest"]) == str(stored["payload_digest"])
        ),
    }
    if type(record) is CandidateRevisionV2:
        return PersistedCandidateRevisionV2(**values)
    if type(record) is CandidateRevisionV3:
        return PersistedCandidateRevisionV3(**values)
    return PersistedCandidateRevisionV4(**values)


__all__ = [
    "CANDIDATE_REVISION_SCHEMA_REPO_PATH",
    "CANDIDATE_REVISION_SCHEMA_VERSION",
    "CANDIDATE_REVISION_V3_SCHEMA_REPO_PATH",
    "CANDIDATE_REVISION_V4_SCHEMA_REPO_PATH",
    "CANDIDATE_REVISION_V3_SCHEMA_VERSION",
    "CANDIDATE_REVISION_V4_SCHEMA_VERSION",
    "CandidateRevision",
    "CandidateRevisionV2",
    "CandidateRevisionV3",
    "CandidateRevisionV4",
    "PersistedCandidateRevision",
    "PersistedCandidateRevisionV2",
    "PersistedCandidateRevisionV3",
    "PersistedCandidateRevisionV4",
    "candidate_a1_requirement",
    "candidate_complete_target_claim",
    "candidate_requires_a1",
    "candidate_schema_digest",
    "candidate_schema_digest_v3",
    "candidate_schema_digest_v4",
    "commit_candidate_revision",
    "prepare_candidate_revision",
    "prepare_candidate_revision_v3",
    "prepare_candidate_revision_v4",
    "read_candidate_revision",
    "validate_candidate_revision",
    "validate_candidate_revision_v2",
    "validate_candidate_revision_v3",
    "validate_candidate_revision_v4",
]
