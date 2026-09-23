"""Lean bridge from a formal Research Session to a Workstation Attempt.

Formal execution is an operational invocation, not a Mathematical Research
semantic operation. Mathematical Research supplies one immutable Session
binding and retains factual output custody. Workstation Control remains the
owner of Attempt identity, effects, execution state, recovery, and the complete
operational result.

The bridge exposes the same explicit prepare, dispatch, and reconcile/settle
lifecycle for narrow callers plus one server-owned synchronous controller for
the Host operational action. It creates no Evidence meaning and never chooses
a Strategy or mathematical interpretation. A retry exists only when the
executive supplies an explicit correction basis.
"""

from __future__ import annotations

import hashlib
import math
import os
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from research_attempt_adapter import (
    AppSelection,
    AttemptIntent,
    AttemptJournal,
    AttemptRecord,
    AttemptResult,
    AttemptState,
    BrowserCapabilityMode,
    CodexExecProvider,
    ExecutionFence,
    FormalSessionBinding,
    LocalCapabilityRoot,
    McpServerSelection,
    NetworkPolicy,
    OutputArtifact,
    ProviderEffectCertainty,
    ResearchAttemptAdapter,
    ResourceRequest,
    ResultArtifact,
    SelectedCapabilities,
    SourceAttachment,
    SourceVerification,
    SourceVerificationError,
    VerifiedOuterContainment,
)

from .candidate_revision import read_candidate_revision
from .context_revision import PersistedContextRevision, read_context_revision
from .evidence_store import EvidenceCAS, EvidenceStoreError
from .formal_session import (
    FormalRequestSelection,
    PersistedFormalSessionRevision,
    _assert_active_authority,
    _current_formal_request,
    _issue_verified_formal_session_result,
    commit_formal_session_creation,
    prepare_formal_session_creation,
    read_formal_session_revision,
    terminate_formal_session,
)
from .json_support import canonical_json_bytes, loads_strict_json_object
from .mission_evidence import (
    RawCaptureArtifactFileInput,
    RawCaptureRecord,
    commit_raw_capture,
    prepare_raw_capture,
    read_evidence_meaning,
    read_raw_capture,
)
from .mission_executive import DirectExecutiveEpochAuthority
from .mission_frontier import read_branch_revision, read_strategy_revision
from .mission_owner import successor_mission_contract
from .workspace_schema import IdentityKind, RevisionRef, TypedWorkspaceId
from .workspace_store import StaleCommandError, WriterLease, WorkspaceIntegrityError


_KNOWN_TERMINAL_STATES = frozenset(
    {
        AttemptState.SUCCEEDED,
        AttemptState.FAILED,
        AttemptState.CANCELLED,
        AttemptState.FORCE_STOPPED,
        AttemptState.FENCED,
    }
)
_RETRYABLE_TERMINAL_STATES = frozenset(
    {
        AttemptState.FAILED,
        AttemptState.CANCELLED,
        AttemptState.FORCE_STOPPED,
    }
)
_SESSION_TERMINAL_STATES = frozenset(
    {
        AttemptState.SUCCEEDED,
        AttemptState.FENCED,
    }
)
_OBSERVATION_DOMAIN = "mathematical_research.formal_attempt_observation.v1"
_ATTEMPT_IDENTITY_DOMAIN = "mathematical_research.formal_attempt.identity.v1"
_ATTEMPT_READINESS_DOMAIN = "mathematical_research.formal_attempt.readiness.v1"
_FENCE_IDENTITY_DOMAIN = "mathematical_research.formal_attempt.fence.v1"
_CONTEXT_PACKAGE_SCHEMA = "mathematical_research.formal_context_package.v1"
_RELEASE_RECORD_SCHEMA = "wc.rh_mission_host_release.v1"
_RELEASE_RECORD_RELATIVE_PATH = Path(".mathematical-research-release.json")
_RELEASE_RECORD_KEYS = frozenset(
    {
        "schema_version",
        "release_sha",
        "bundle_sha256",
        "bundle_size",
        "node_version",
        "pnpm_version",
        "codex_version",
        "service_package",
    }
)
_RELEASE_SERVICE_PACKAGE = "@workstation-control/rh-mission-host"
_FORMAL_PROVIDER_ID = "codex_exec"
_FORMAL_PROVIDER_PROFILE = "codex-provider.v2"
_FORMAL_MODEL_CATALOG_RELEASE_RELATIVE_PATH = Path(
    "services/rh-mission-host/assets/"
    "codex-model-catalog.0.153.4.json"
)
_FORMAL_ACTOR = "rh-mission-formal-operation"
_LEGACY_PROVIDER_OPERATIONAL_OUTPUT_PATHS = frozenset(
    {
        "app-server-events.jsonl",
        "app-server-stderr.log",
    }
)
_ACTIVE_ATTEMPT_STATES = frozenset(
    {
        AttemptState.PREPARED,
        AttemptState.LAUNCH_REQUESTED,
        AttemptState.RUNNING,
        AttemptState.CANCEL_REQUESTED,
        AttemptState.FORCE_STOP_REQUESTED,
    }
)


class FormalAttemptBridgeError(RuntimeError):
    """Fail-closed formal-execution boundary error with a stable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class FormalAttemptRuntimeFacts:
    """Trusted Host facts used to construct, never model-select, formal execution."""

    release_root: Path
    release_commit: str
    state_root: Path
    codex_executable: Path
    codex_home: Path
    outer_containment_id: str
    trusted_mcp_server_ids: tuple[str, ...] = ()
    trusted_app_ids: tuple[str, ...] = ()
    polling_cadence_seconds: float = 0.25

    def __post_init__(self) -> None:
        for name in (
            "release_root",
            "state_root",
            "codex_executable",
            "codex_home",
        ):
            value = Path(getattr(self, name))
            if not value.is_absolute():
                raise ValueError(f"{name} must be one Host-selected absolute path")
            object.__setattr__(self, name, Path(os.path.abspath(value)))
        if (
            len(self.release_commit) not in {40, 64}
            or any(character not in "0123456789abcdef" for character in self.release_commit)
        ):
            raise ValueError("release_commit must be one exact lowercase Git commit")
        if not self.outer_containment_id or any(
            character in self.outer_containment_id for character in "\x00\r\n"
        ):
            raise ValueError("outer_containment_id must be one exact Host identity")
        for name in ("trusted_mcp_server_ids", "trusted_app_ids"):
            values = tuple(getattr(self, name))
            if (
                any(not isinstance(item, str) or not item.strip() for item in values)
                or len(values) != len(set(values))
            ):
                raise ValueError(f"{name} must be unique non-empty server facts")
            object.__setattr__(self, name, values)
        cadence = self.polling_cadence_seconds
        if (
            isinstance(cadence, bool)
            or not isinstance(cadence, (int, float))
            or not math.isfinite(float(cadence))
            or cadence <= 0
        ):
            raise ValueError(
                "polling_cadence_seconds must be a positive transport cadence"
            )
        object.__setattr__(self, "polling_cadence_seconds", float(cadence))
        _require_ordinary_directory(self.release_root, "immutable release root")
        _require_ordinary_directory(self.codex_home, "protected Codex home")
        if not self.codex_executable.is_file():
            raise ValueError("codex_executable must be one exact existing file")
        roots = (self.release_root, self.state_root, self.codex_home)
        if any(
            _paths_overlap(first, second)
            for index, first in enumerate(roots)
            for second in roots[index + 1 :]
        ):
            raise ValueError(
                "release, formal state, and protected Codex roots must be disjoint"
            )

    @property
    def outer_containment(self) -> VerifiedOuterContainment:
        material = {
            "schema": "wc.formal_attempt_outer_containment.v1",
            "boundary_id": self.outer_containment_id,
            "public_only_egress": True,
            "private_network_denied": True,
            "metadata_denied": True,
            "inbound_denied": True,
        }
        return VerifiedOuterContainment(
            boundary_id=self.outer_containment_id,
            boundary_digest=hashlib.sha256(canonical_json_bytes(material)).hexdigest(),
            public_only_egress=True,
            private_network_denied=True,
            metadata_denied=True,
            inbound_denied=True,
        )


class FormalAttemptControl:
    """In-process operator control; force is explicit and never time-escalated."""

    def __init__(self) -> None:
        self._requested: str | None = None

    def request_cancel(self) -> None:
        if self._requested is None:
            self._requested = "cancel"

    def request_force_stop(self) -> None:
        self._requested = "force_stop"

    @property
    def requested(self) -> str | None:
        return self._requested


def _paths_overlap(first: Path, second: Path) -> bool:
    first_key = Path(os.path.normcase(os.path.abspath(first)))
    second_key = Path(os.path.normcase(os.path.abspath(second)))
    return (
        first_key == second_key
        or first_key in second_key.parents
        or second_key in first_key.parents
    )


def _require_ordinary_directory(path: Path, label: str) -> None:
    try:
        details = path.lstat()
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    if (
        path.is_symlink()
        or bool(getattr(details, "st_file_attributes", 0) & reparse)
        or not stat.S_ISDIR(details.st_mode)
    ):
        raise ValueError(f"{label} must be an ordinary non-link directory")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def installed_release_source_digest(root: Path, release_commit: str) -> str:
    """Read the installer-owned source identity for one enriched release."""

    root = Path(os.path.abspath(root))
    _require_ordinary_directory(root, "installed release root")
    record_path = root / _RELEASE_RECORD_RELATIVE_PATH
    try:
        before = record_path.lstat()
        reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if (
            record_path.is_symlink()
            or bool(getattr(before, "st_file_attributes", 0) & reparse)
            or not stat.S_ISREG(before.st_mode)
        ):
            raise ValueError("release record is not one ordinary file")
        record = loads_strict_json_object(record_path.read_bytes())
        after = record_path.lstat()
    except (OSError, TypeError, UnicodeError, ValueError) as exc:
        raise FormalAttemptBridgeError(
            "release_source_identity_invalid",
            "Installed release record is unavailable or invalid.",
        ) from exc
    if (
        (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    ):
        raise FormalAttemptBridgeError(
            "release_source_identity_changed",
            "Installed release record changed while it was read.",
        )
    bundle_sha256 = record.get("bundle_sha256")
    if (
        set(record) != _RELEASE_RECORD_KEYS
        or record.get("schema_version") != _RELEASE_RECORD_SCHEMA
        or record.get("release_sha") != release_commit
        or root.name != release_commit
        or record.get("service_package") != _RELEASE_SERVICE_PACKAGE
        or not isinstance(bundle_sha256, str)
        or len(bundle_sha256) != 64
        or any(character not in "0123456789abcdef" for character in bundle_sha256)
        or not isinstance(record.get("bundle_size"), int)
        or isinstance(record.get("bundle_size"), bool)
        or int(record["bundle_size"]) <= 0
        or any(
            not isinstance(record.get(key), str) or not str(record[key]).strip()
            for key in ("node_version", "pnpm_version", "codex_version")
        )
    ):
        raise FormalAttemptBridgeError(
            "release_source_identity_invalid",
            "Installed release record does not bind the exact selected source.",
        )
    return bundle_sha256


class _InstalledReleaseSourceVerifier:
    verifier_id = "installed_release_bundle_sha256_v1"

    def __init__(self, facts: FormalAttemptRuntimeFacts, source_digest: str) -> None:
        self._facts = facts
        self._source_digest = source_digest

    def verify(self, intent: AttemptIntent) -> SourceVerification:
        if (
            Path(intent.working_directory) != self._facts.release_root
            or intent.source_commit != self._facts.release_commit
            or intent.source_digest != self._source_digest
        ):
            raise SourceVerificationError(
                "release_source_binding_mismatch",
                "Attempt source differs from the Host-selected immutable release.",
            )
        if (
            installed_release_source_digest(
                self._facts.release_root, self._facts.release_commit
            )
            != self._source_digest
        ):
            raise SourceVerificationError(
                "release_source_changed",
                "Installed release source identity changed before provider execution.",
            )
        return SourceVerification(
            verifier_id=self.verifier_id,
            source_commit=self._facts.release_commit,
            source_digest=self._source_digest,
        )


@dataclass(frozen=True, slots=True)
class FormalAttemptReconciliation:
    """JSON-friendly facts from one verified reconciliation and optional settlement."""

    attempt_result: AttemptResult
    raw_capture: RawCaptureRecord | None
    session_was_terminal: bool
    terminal_outcome: Any | None

    @property
    def session_is_terminal(self) -> bool:
        return self.session_was_terminal or self.terminal_outcome is not None

    def to_payload(self) -> dict[str, Any]:
        capture_ref = (
            None
            if self.raw_capture is None
            else {
                "capture_id": self.raw_capture.capture_id,
                "capture_digest_sha256": self.raw_capture.digest_sha256,
            }
        )
        return {
            "schema": "mr.formal_attempt_reconciliation.v1",
            "session_id": self.attempt_result.session_id,
            "attempt_id": self.attempt_result.attempt_id,
            "attempt_state": self.attempt_result.attempt_state.value,
            "provider_effect_certainty": (
                self.attempt_result.provider_effect_certainty.value
            ),
            "result_digest_sha256": self.attempt_result.result_digest,
            "raw_capture": capture_ref,
            "session_was_terminal": self.session_was_terminal,
            "session_is_terminal": self.session_is_terminal,
            "result_bound_to_session": self.terminal_outcome is not None,
            "terminal_transition_replayed": (
                None
                if self.terminal_outcome is None
                else bool(getattr(self.terminal_outcome, "replayed", False))
            ),
        }


def _owner_ref_text(reference: Mapping[str, Any]) -> str:
    return canonical_json_bytes(reference).decode("ascii")


def formal_session_binding(
    session: PersistedFormalSessionRevision,
) -> FormalSessionBinding:
    """Project one persisted open revision-one Session into WC's opaque binding."""

    if type(session) is not PersistedFormalSessionRevision:
        raise TypeError(
            "formal Session binding requires PersistedFormalSessionRevision"
        )
    document = session.record.document
    if (
        session.revision != 1
        or session.predecessor_revision is not None
        or document["lifecycle"] != "open"
        or document["terminal_binding"] is not None
    ):
        raise FormalAttemptBridgeError(
            "formal_session_genesis_invalid",
            "Workstation execution requires the exact persisted open Session revision one.",
        )
    return FormalSessionBinding(
        session_id=str(document["session_id"]),
        session_digest=session.record.payload_sha256,
        mission_ref=_owner_ref_text(document["mission_ref"]),
        strategy_ref=_owner_ref_text(document["strategy_ref"]),
        selected_bet_sha256=str(document["selected_bet_sha256"]),
        context_ref=_owner_ref_text(document["context_ref"]),
    )


def _write_exact_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    for parent in (path.parent, *path.parent.parents):
        if parent == path.anchor:
            break
        if parent.exists() and parent.is_symlink():
            raise FormalAttemptBridgeError(
                "formal_context_path_unsafe",
                "Formal Context package contains a linked parent.",
            )
    try:
        with path.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if (
            path.is_symlink()
            or not path.is_file()
            or path.stat().st_size != len(content)
            or _sha256_file(path) != hashlib.sha256(content).hexdigest()
        ):
            raise FormalAttemptBridgeError(
                "formal_context_package_collision",
                "Persisted formal Context package differs from the exact owner bytes.",
            )


def _json_attachment(
    package_root: Path,
    logical_name: str,
    value: Mapping[str, Any],
) -> SourceAttachment:
    content = canonical_json_bytes(value)
    path = package_root.joinpath(*logical_name.split("/"))
    _write_exact_bytes(path, content)
    return SourceAttachment(
        logical_name=logical_name,
        source_path=str(path),
        sha256=hashlib.sha256(content).hexdigest(),
        byte_length=len(content),
        media_type="application/json",
        encoding="utf-8",
    )


def _verified_owner_document(
    store: Any,
    *,
    mission_id: str,
    reference: Mapping[str, Any],
) -> tuple[Mapping[str, Any], Any | None]:
    kind = str(reference["kind"])
    identity = str(reference["identity"])
    revision = int(reference["revision"])
    expected_digest = str(reference["payload_sha256"])
    evidence_meaning = None
    if kind == "mission":
        if identity != mission_id:
            raise StaleCommandError("formal Context references another Mission")
        stored = store.get_revision(
            RevisionRef(TypedWorkspaceId(IdentityKind.MISSION, identity), revision)
        )
        if stored is None:
            raise StaleCommandError("formal Context Mission revision is absent")
        document = stored.payload
        actual_digest = stored.payload_digest
    elif kind == "strategy":
        persisted = read_strategy_revision(
            store,
            mission_id=mission_id,
            strategy_id=identity,
            revision=revision,
        )
        document = persisted.record.document
        actual_digest = persisted.record.payload_sha256
    elif kind == "branch":
        persisted = read_branch_revision(
            store,
            mission_id=mission_id,
            branch_id=identity,
            revision=revision,
        )
        document = persisted.record.document
        actual_digest = persisted.record.payload_sha256
    elif kind == "candidate":
        persisted = read_candidate_revision(
            store,
            mission_id=mission_id,
            candidate_id=identity,
            revision=revision,
        )
        document = persisted.record.document
        actual_digest = persisted.payload_digest
    elif kind == "context":
        persisted = read_context_revision(
            store,
            context_id=identity,
            revision=revision,
        )
        if persisted.record.document["mission_id"] != mission_id:
            raise StaleCommandError("formal Context source belongs to another Mission")
        document = persisted.record.document
        actual_digest = persisted.payload_digest
    elif kind == "evidence":
        evidence_meaning = read_evidence_meaning(
            store,
            evidence_id=identity,
            revision=revision,
        )
        if evidence_meaning.evidence.subject.get("mission_id") != mission_id:
            raise StaleCommandError("formal Context Evidence belongs to another Mission")
        document = evidence_meaning.evidence.to_payload()
        actual_digest = evidence_meaning.payload_digest
    else:
        raise FormalAttemptBridgeError(
            "formal_context_owner_unsupported",
            f"Formal Context source kind {kind!r} has no direct owner reader.",
        )
    if (
        actual_digest != expected_digest
        or hashlib.sha256(canonical_json_bytes(document)).hexdigest() != expected_digest
    ):
        raise WorkspaceIntegrityError(
            "formal Context owner revision differs from its exact reference"
        )
    return document, evidence_meaning


def _stage_formal_context_package(
    store: Any,
    *,
    cas: EvidenceCAS,
    authority: DirectExecutiveEpochAuthority,
    selection: FormalRequestSelection,
    context: PersistedContextRevision,
    session: PersistedFormalSessionRevision,
    package_root: Path,
) -> tuple[SourceAttachment, ...]:
    """Stage exact owner documents and referenced Capture bytes without ceilings."""

    package_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _require_ordinary_directory(package_root, "formal Context package root")
    attachments: dict[str, SourceAttachment] = {}

    def add_json(logical_name: str, value: Mapping[str, Any]) -> None:
        attachment = _json_attachment(package_root, logical_name, value)
        prior = attachments.get(logical_name)
        if prior is not None and prior != attachment:
            raise FormalAttemptBridgeError(
                "formal_context_logical_name_collision",
                "Formal Context package logical names are ambiguous.",
            )
        attachments[logical_name] = attachment

    add_json(
        "formal/request.json",
        {
            "schema": _CONTEXT_PACKAGE_SCHEMA,
            "session_id": session.record.document["session_id"],
            "session_digest_sha256": session.record.payload_sha256,
            "purpose": selection.purpose,
            "strategy_ref": selection.strategy_ref,
            "selected_bet_sha256": selection.selected_bet_sha256,
            "selected_bet": selection.selected_bet,
            "context_ref": selection.context_ref,
        },
    )
    add_json("formal/context.json", context.record.document)

    seen_owners: set[tuple[str, str, int, str]] = set()
    seen_captures: set[str] = set()
    seen_blob_paths: set[tuple[str, str]] = set()

    def add_capture(capture_id: str, artifact_ordinal: int) -> None:
        capture = read_raw_capture(store, capture_id=capture_id)
        if capture.mission_id != authority.mission_id:
            raise StaleCommandError("formal Context Capture belongs to another Mission")
        capture_key = hashlib.sha256(capture_id.encode("utf-8")).hexdigest()
        if capture_id not in seen_captures:
            seen_captures.add(capture_id)
            add_json(
                f"captures/{capture_key}/capture.json",
                capture.to_payload(),
            )
        try:
            descriptor = capture.artifacts[artifact_ordinal]
        except IndexError as exc:
            raise WorkspaceIntegrityError(
                "formal Context Evidence names an absent Capture artifact"
            ) from exc
        if descriptor.ordinal != artifact_ordinal:
            raise WorkspaceIntegrityError(
                "formal Context Capture artifact ordinals are not exact"
            )
        source = cas.path_for_digest(descriptor.blob_sha256)
        try:
            byte_length = source.stat().st_size
            cas.verify_exact_file_source(
                source,
                expected_sha256=descriptor.blob_sha256,
                expected_length=byte_length,
            )
        except (EvidenceStoreError, OSError) as exc:
            raise WorkspaceIntegrityError(
                "formal Context Capture bytes are unavailable or changed"
            ) from exc
        source_key = (os.path.normcase(str(source)), descriptor.blob_sha256)
        if source_key in seen_blob_paths:
            return
        seen_blob_paths.add(source_key)
        logical_name = (
            f"captures/{capture_key}/{artifact_ordinal}-{descriptor.blob_sha256}.bin"
        )
        attachments[logical_name] = SourceAttachment(
            logical_name=logical_name,
            source_path=str(source),
            sha256=descriptor.blob_sha256,
            byte_length=byte_length,
            media_type="application/octet-stream",
            encoding=None,
        )

    def add_owner(reference: Mapping[str, Any]) -> None:
        key = (
            str(reference["kind"]),
            str(reference["identity"]),
            int(reference["revision"]),
            str(reference["payload_sha256"]),
        )
        if key in seen_owners:
            return
        seen_owners.add(key)
        document, evidence_meaning = _verified_owner_document(
            store,
            mission_id=authority.mission_id,
            reference=reference,
        )
        kind, _identity, _revision, digest = key
        add_json(f"owners/{kind}/{digest}.json", document)
        if evidence_meaning is None:
            return
        add_json(
            f"owners/evidence/{digest}.sources.json",
            {
                "evidence_ref": reference,
                "capture_sources": [
                    item.to_payload(index)
                    for index, item in enumerate(evidence_meaning.sources)
                ],
                "interpreted_owner_inputs": [
                    item.to_payload() for item in evidence_meaning.interpreted_inputs
                ],
            },
        )
        for source in evidence_meaning.sources:
            add_capture(source.capture_id, source.artifact_ordinal)
        for interpreted in evidence_meaning.interpreted_inputs:
            add_owner(interpreted.to_payload())

    for source in context.record.document["owner_source_references"]:
        add_owner(source["reference"])
    return tuple(attachments[name] for name in sorted(attachments))


def _mission_policy_and_capabilities(
    store: Any,
    *,
    authority: DirectExecutiveEpochAuthority,
    runtime: FormalAttemptRuntimeFacts,
) -> tuple[str, str, tuple[str, ...], SelectedCapabilities]:
    mission_ref = authority.mission_root
    stored = store.get_revision(
        RevisionRef(
            TypedWorkspaceId(IdentityKind.MISSION, authority.mission_id),
            int(mission_ref["revision"]),
        )
    )
    if stored is None or stored.payload_digest != mission_ref["payload_sha256"]:
        raise StaleCommandError("formal Attempt Mission root is absent or changed")
    mission = stored.payload
    contract = successor_mission_contract(
        {
            "purpose": mission["purpose"],
            "execution_policy": mission["execution_policy"],
        }
    )
    policy = contract["execution_policy"]
    baseline = tuple(
        item for item in policy["baseline_capabilities"] if item != "rh_mission"
    )
    selected = policy["selected_capabilities"]
    mcp_servers = tuple(
        McpServerSelection(
            server_id=str(item["server_id"]),
            enabled_tools=tuple(str(tool) for tool in item["enabled_tools"]),
        )
        for item in selected["mcp_servers"]
    )
    apps = tuple(
        AppSelection(
            app_id=str(item["app_id"]),
            enabled_tools=tuple(str(tool) for tool in item["enabled_tools"]),
        )
        for item in selected["apps"]
    )
    if not {item.server_id for item in mcp_servers}.issubset(
        runtime.trusted_mcp_server_ids
    ):
        raise FormalAttemptBridgeError(
            "formal_mcp_selection_untrusted",
            "Mission-selected MCP server is not one Host-trusted preconfiguration.",
        )
    if not {item.app_id for item in apps}.issubset(runtime.trusted_app_ids):
        raise FormalAttemptBridgeError(
            "formal_app_selection_untrusted",
            "Mission-selected app is not one Host-trusted preconfiguration.",
        )
    capabilities = SelectedCapabilities(
        local_roots=tuple(
            LocalCapabilityRoot(
                kind=str(item["kind"]),
                id=str(item["id"]),
                release_relative_path=str(item["release_relative_path"]),
            )
            for item in selected["local_roots"]
        ),
        mcp_servers=mcp_servers,
        apps=apps,
        browser=(
            None
            if selected["browser"] is None
            else BrowserCapabilityMode.ISOLATED_EPHEMERAL_UNAUTHENTICATED
        ),
    )
    return str(policy["model"]), str(policy["reasoning_effort"]), baseline, capabilities


def build_formal_attempt_adapter(
    *,
    cas: EvidenceCAS,
    runtime: FormalAttemptRuntimeFacts,
) -> ResearchAttemptAdapter:
    """Build the sole WC adapter owner over durable Host-selected roots."""

    if type(cas) is not EvidenceCAS:
        raise TypeError("formal Attempt adapter requires the owning EvidenceCAS")
    runtime.state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    _require_ordinary_directory(runtime.state_root, "formal Attempt state root")
    context_root = runtime.state_root / "context-packages"
    context_root.mkdir(mode=0o700, exist_ok=True)
    source_digest = installed_release_source_digest(
        runtime.release_root, runtime.release_commit
    )
    provider = CodexExecProvider(
        state_root=runtime.state_root / "provider",
        model_catalog_path=(
            runtime.release_root / _FORMAL_MODEL_CATALOG_RELEASE_RELATIVE_PATH
        ),
        executable=str(runtime.codex_executable),
        environment={
            "CODEX_HOME": str(runtime.codex_home),
            **(
                {"PATH": os.environ["PATH"]}
                if isinstance(os.environ.get("PATH"), str)
                else {}
            ),
        },
        outer_containment=runtime.outer_containment,
        trusted_mcp_server_ids=runtime.trusted_mcp_server_ids,
        trusted_app_ids=runtime.trusted_app_ids,
        polling_cadence_seconds=runtime.polling_cadence_seconds,
    )
    source_roots: tuple[Path, ...] = (context_root,)
    if cas.cas_root.exists():
        source_roots += (cas.cas_root,)
    return ResearchAttemptAdapter(
        journal=AttemptJournal(runtime.state_root / "attempt-journal.sqlite3"),
        providers={provider.provider_id: provider},
        source_verifier=_InstalledReleaseSourceVerifier(runtime, source_digest),
        input_stage_store_root=runtime.state_root / "input-stages",
        scratch_store_root=runtime.state_root / "scratch",
        provider_output_root=runtime.state_root / "provider-output",
        artifact_store_root=runtime.state_root / "sealed-output",
        protected_root=runtime.codex_home,
        source_attachment_roots=source_roots,
        outer_containment=runtime.outer_containment,
    )


def _domain_digest(domain: str, value: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        canonical_json_bytes({"domain": domain, **dict(value)})
    ).hexdigest()


def _attempt_id(
    binding: FormalSessionBinding,
    *,
    previous_attempt_id: str | None,
    correction_basis: str | None,
) -> str:
    digest = _domain_digest(
        _ATTEMPT_IDENTITY_DOMAIN,
        {
            "session_id": binding.session_id,
            "session_digest_sha256": binding.session_digest,
            "previous_attempt_id": previous_attempt_id,
            "correction_basis": correction_basis,
        },
    )
    return f"attempt.formal.{digest}"


def _attempts_for_session(
    adapter: ResearchAttemptAdapter,
    binding: FormalSessionBinding,
) -> tuple[AttemptRecord, ...]:
    records = tuple(
        item
        for item in adapter.journal.list_attempts()
        if item.session_id == binding.session_id
    )
    for record in records:
        _require_record_binding(record, binding)
    return records


def _selected_attempt_lineage(
    adapter: ResearchAttemptAdapter,
    *,
    binding: FormalSessionBinding,
    correction_basis: str | None,
) -> tuple[str, str | None, str | None]:
    records = _attempts_for_session(adapter, binding)
    if correction_basis is None:
        attempt_id = _attempt_id(
            binding,
            previous_attempt_id=None,
            correction_basis=None,
        )
        return attempt_id, None, None
    if not isinstance(correction_basis, str) or not correction_basis.strip():
        raise FormalAttemptBridgeError(
            "formal_retry_basis_invalid",
            "An explicit retry correction basis must be non-empty text.",
        )
    if not records:
        raise FormalAttemptBridgeError(
            "formal_retry_predecessor_absent",
            "A correction basis cannot create a first formal Attempt.",
        )
    latest = records[-1]
    if latest.intent.correction_basis == correction_basis:
        previous_attempt_id = latest.intent.previous_attempt_id
        return latest.attempt_id, previous_attempt_id, correction_basis
    return (
        _attempt_id(
            binding,
            previous_attempt_id=latest.attempt_id,
            correction_basis=correction_basis,
        ),
        latest.attempt_id,
        correction_basis,
    )


def _attempt_intent(
    *,
    binding: FormalSessionBinding,
    authority: DirectExecutiveEpochAuthority,
    runtime: FormalAttemptRuntimeFacts,
    source_digest: str,
    attachments: tuple[SourceAttachment, ...],
    model_profile: str,
    reasoning_effort: str,
    baseline_capabilities: tuple[str, ...],
    selected_capabilities: SelectedCapabilities,
    attempt_id: str,
    previous_attempt_id: str | None,
    correction_basis: str | None,
) -> AttemptIntent:
    readiness_digest = _domain_digest(
        _ATTEMPT_READINESS_DOMAIN,
        {
            "session_id": binding.session_id,
            "session_digest_sha256": binding.session_digest,
            "source_commit": runtime.release_commit,
            "source_digest_sha256": source_digest,
            "correction_basis": correction_basis,
            "context_files": [
                {
                    "logical_name": item.logical_name,
                    "sha256": item.sha256,
                    "byte_length": item.byte_length,
                }
                for item in attachments
            ],
        },
    )
    return AttemptIntent(
        attempt_id=attempt_id,
        session_id=binding.session_id,
        session_digest=binding.session_digest,
        previous_attempt_id=previous_attempt_id,
        correction_basis=correction_basis,
        readiness_state_digest=readiness_digest,
        coordination_id=authority.executive_epoch_id,
        coordination_digest=authority.bound_event_digest,
        authorization_id=f"authority.{authority.executive_epoch_id}",
        authorization_digest=authority.authority_sha256,
        source_commit=runtime.release_commit,
        source_digest=source_digest,
        provider_id=_FORMAL_PROVIDER_ID,
        provider_profile=_FORMAL_PROVIDER_PROFILE,
        model_profile=model_profile,
        reasoning_effort=reasoning_effort,
        resource_request=ResourceRequest(),
        baseline_capabilities=baseline_capabilities,
        network_policy=NetworkPolicy.PUBLIC,
        outer_containment_id=runtime.outer_containment.boundary_id,
        outer_containment_digest=runtime.outer_containment.boundary_digest,
        source_attachments=attachments,
        working_directory=str(runtime.release_root),
        selected_capabilities=selected_capabilities,
    )


def _execution_fence(intent: AttemptIntent) -> ExecutionFence:
    digest = _domain_digest(
        _FENCE_IDENTITY_DOMAIN,
        {
            "attempt_id": intent.attempt_id,
            "provider_id": intent.provider_id,
            "coordination_id": intent.coordination_id,
            "coordination_digest": intent.coordination_digest,
            "authorization_id": intent.authorization_id,
            "authorization_digest": intent.authorization_digest,
            "source_commit": intent.source_commit,
            "source_digest": intent.source_digest,
        },
    )
    return ExecutionFence(
        fence_id=f"fence.formal.{digest}",
        provider_id=intent.provider_id,
        coordination_id=intent.coordination_id,
        coordination_digest=intent.coordination_digest,
        authorization_id=intent.authorization_id,
        authorization_digest=intent.authorization_digest,
        source_commit=intent.source_commit,
        source_digest=intent.source_digest,
    )


def _apply_requested_control(
    adapter: ResearchAttemptAdapter,
    record: AttemptRecord,
    control: FormalAttemptControl,
) -> AttemptRecord:
    requested = control.requested
    if requested == "force_stop" and record.state in _ACTIVE_ATTEMPT_STATES:
        return adapter.force_stop(record.attempt_id)
    if requested == "cancel" and record.state in _ACTIVE_ATTEMPT_STATES:
        return adapter.request_cancel(record.attempt_id)
    return record


def execute_formal_attempt_operation(
    store: Any,
    *,
    cas: EvidenceCAS,
    authority: DirectExecutiveEpochAuthority,
    adapter: ResearchAttemptAdapter,
    runtime: FormalAttemptRuntimeFacts,
    selected_bet_sha256: str,
    correction_basis: str | None,
    lease: WriterLease,
    actor: str = _FORMAL_ACTOR,
    control: FormalAttemptControl | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> FormalAttemptReconciliation:
    """Run/reconcile one explicit formal action until terminal or factual UNKNOWN."""

    _require_adapter(adapter)
    if type(runtime) is not FormalAttemptRuntimeFacts:
        raise TypeError("formal Attempt operation requires trusted runtime facts")
    if type(control) is not FormalAttemptControl:
        if control is not None:
            raise TypeError("formal Attempt control must be exact or omitted")
        control = FormalAttemptControl()
    if not callable(sleep):
        raise TypeError("formal Attempt polling requires an injected sleep function")

    _mission_ref, _strategy, selection, context = _current_formal_request(
        store,
        authority=authority,
        selected_bet_sha256=selected_bet_sha256,
    )
    prepared_session = prepare_formal_session_creation(
        store,
        authority=authority,
        formal_request=selection,
    )
    commit_formal_session_creation(
        store,
        authority=authority,
        prepared=prepared_session,
        lease=lease,
        actor=actor,
    )
    session_id = str(prepared_session.record.document["session_id"])
    current_session = read_formal_session_revision(
        store,
        mission_id=authority.mission_id,
        session_id=session_id,
    )
    genesis = (
        current_session
        if current_session.revision == 1
        else read_formal_session_revision(
            store,
            mission_id=authority.mission_id,
            session_id=session_id,
            revision=1,
        )
    )
    binding = formal_session_binding(genesis)

    if current_session.record.document["lifecycle"] == "terminal":
        terminal_binding = current_session.record.document["terminal_binding"]
        assert terminal_binding is not None
        target_attempt_id = str(
            terminal_binding["attempt_result_ref"]["attempt_id"]
        )
        record = adapter.journal.get_attempt(target_attempt_id)
        _require_record_binding(record, binding)
        if record.intent.correction_basis != correction_basis:
            raise FormalAttemptBridgeError(
                "formal_session_terminal",
                "Terminal Session can replay only the exact original formal action.",
            )
        return reconcile_and_settle_formal_attempt(
            store,
            cas=cas,
            authority=authority,
            adapter=adapter,
            session_id=session_id,
            attempt_id=target_attempt_id,
            lease=lease,
            actor=actor,
        )

    package_root = runtime.state_root / "context-packages" / binding.session_digest
    attachments = _stage_formal_context_package(
        store,
        cas=cas,
        authority=authority,
        selection=selection,
        context=context,
        session=genesis,
        package_root=package_root,
    )
    model_profile, reasoning_effort, baseline, selected_capabilities = (
        _mission_policy_and_capabilities(
            store,
            authority=authority,
            runtime=runtime,
        )
    )
    source_digest = installed_release_source_digest(
        runtime.release_root, runtime.release_commit
    )
    attempt_id, previous_attempt_id, exact_correction_basis = (
        _selected_attempt_lineage(
            adapter,
            binding=binding,
            correction_basis=correction_basis,
        )
    )
    intent = _attempt_intent(
        binding=binding,
        authority=authority,
        runtime=runtime,
        source_digest=source_digest,
        attachments=attachments,
        model_profile=model_profile,
        reasoning_effort=reasoning_effort,
        baseline_capabilities=baseline,
        selected_capabilities=selected_capabilities,
        attempt_id=attempt_id,
        previous_attempt_id=previous_attempt_id,
        correction_basis=exact_correction_basis,
    )
    fence = _execution_fence(intent)
    adapter.install_fence(fence)
    record = prepare_formal_attempt(
        store,
        authority=authority,
        adapter=adapter,
        session_id=session_id,
        intent=intent,
        fence_id=fence.fence_id,
    )
    record = _apply_requested_control(adapter, record, control)
    if record.state is AttemptState.PREPARED:
        record = dispatch_formal_attempt(
            store,
            authority=authority,
            adapter=adapter,
            session_id=session_id,
            attempt_id=attempt_id,
        )

    while True:
        record = _apply_requested_control(adapter, record, control)
        reconciliation = reconcile_and_settle_formal_attempt(
            store,
            cas=cas,
            authority=authority,
            adapter=adapter,
            session_id=session_id,
            attempt_id=attempt_id,
            lease=lease,
            actor=actor,
        )
        state = reconciliation.attempt_result.attempt_state
        if state is AttemptState.UNKNOWN or state not in _ACTIVE_ATTEMPT_STATES:
            return reconciliation
        record = adapter.journal.get_attempt(attempt_id)
        sleep(runtime.polling_cadence_seconds)


def _session_state(
    store: Any,
    *,
    authority: DirectExecutiveEpochAuthority,
    session_id: str,
) -> tuple[
    FormalSessionBinding,
    PersistedFormalSessionRevision,
    PersistedFormalSessionRevision,
]:
    if type(authority) is not DirectExecutiveEpochAuthority:
        raise TypeError("formal Attempt execution requires direct Epoch authority")
    authority.verify_integrity()
    current = read_formal_session_revision(
        store,
        mission_id=authority.mission_id,
        session_id=session_id,
    )
    genesis = (
        current
        if current.revision == 1
        else read_formal_session_revision(
            store,
            mission_id=authority.mission_id,
            session_id=session_id,
            revision=1,
        )
    )
    document = genesis.record.document
    if (
        document["project_id"] != authority.project_id
        or document["mission_id"] != authority.mission_id
        or document["session_id"] != session_id
    ):
        raise FormalAttemptBridgeError(
            "formal_session_authority_mismatch",
            "The persisted formal Session belongs to another authority boundary.",
        )
    return formal_session_binding(genesis), genesis, current


def _require_open_head(current: PersistedFormalSessionRevision) -> None:
    if current.revision != 1 or current.record.document["lifecycle"] != "open":
        raise FormalAttemptBridgeError(
            "formal_session_not_open",
            "A new provider effect cannot begin after the formal Session is terminal.",
        )


def _require_adapter(adapter: ResearchAttemptAdapter) -> None:
    if type(adapter) is not ResearchAttemptAdapter:
        raise TypeError(
            "formal Attempt execution requires the owning ResearchAttemptAdapter"
        )


def _require_active_session_authority(
    store: Any,
    *,
    authority: DirectExecutiveEpochAuthority,
    genesis: PersistedFormalSessionRevision,
) -> None:
    current_mission_ref = _assert_active_authority(store, authority)
    if current_mission_ref != genesis.record.document["mission_ref"]:
        raise FormalAttemptBridgeError(
            "formal_session_mission_root_stale",
            "The formal Session does not bind the active Mission root.",
        )


def _require_record_binding(
    record: AttemptRecord,
    binding: FormalSessionBinding,
) -> None:
    if record.session != binding:
        raise FormalAttemptBridgeError(
            "workstation_session_binding_mismatch",
            "The Workstation Attempt does not bind the exact persisted Session genesis.",
        )


def prepare_formal_attempt(
    store: Any,
    *,
    authority: DirectExecutiveEpochAuthority,
    adapter: ResearchAttemptAdapter,
    session_id: str,
    intent: AttemptIntent,
    fence_id: str,
) -> AttemptRecord:
    """Durably prepare a first Attempt or explicit corrected retry, without effect."""

    if type(intent) is not AttemptIntent:
        raise TypeError("formal Attempt preparation requires an exact AttemptIntent")
    _require_adapter(adapter)
    binding, genesis, current = _session_state(
        store,
        authority=authority,
        session_id=session_id,
    )
    _require_active_session_authority(
        store,
        authority=authority,
        genesis=genesis,
    )
    _require_open_head(current)
    if (
        intent.session_id != binding.session_id
        or intent.session_digest != binding.session_digest
    ):
        raise FormalAttemptBridgeError(
            "attempt_intent_session_mismatch",
            "AttemptIntent must carry the exact bridge-constructed Session identity and digest.",
        )
    if intent.previous_attempt_id is None:
        record = adapter.prepare(binding, intent, fence_id=fence_id)
    else:
        previous = adapter.journal.get_attempt(intent.previous_attempt_id)
        _require_record_binding(previous, binding)
        record = adapter.retry(
            intent.previous_attempt_id,
            intent,
            fence_id=fence_id,
        )
    _require_record_binding(record, binding)
    return record


def dispatch_formal_attempt(
    store: Any,
    *,
    authority: DirectExecutiveEpochAuthority,
    adapter: ResearchAttemptAdapter,
    session_id: str,
    attempt_id: str,
) -> AttemptRecord:
    """Dispatch or idempotently reconcile one already prepared exact Attempt."""

    _require_adapter(adapter)
    binding, genesis, current = _session_state(
        store,
        authority=authority,
        session_id=session_id,
    )
    _require_active_session_authority(
        store,
        authority=authority,
        genesis=genesis,
    )
    _require_open_head(current)
    prepared = adapter.journal.get_attempt(attempt_id)
    _require_record_binding(prepared, binding)
    dispatched = adapter.dispatch(attempt_id)
    _require_record_binding(dispatched, binding)
    return dispatched


def _require_exact_result(
    *,
    result: AttemptResult,
    binding: FormalSessionBinding,
    attempt_id: str,
    sealed_artifacts: tuple[OutputArtifact, ...],
) -> None:
    if (
        result.attempt_id != attempt_id
        or result.session_id != binding.session_id
        or result.session_digest != binding.session_digest
    ):
        raise FormalAttemptBridgeError(
            "workstation_result_binding_mismatch",
            "The verified Workstation result differs from the original Session binding.",
        )
    declared = tuple(item.artifact for item in result.artifacts)
    if declared != sealed_artifacts:
        raise FormalAttemptBridgeError(
            "workstation_sealed_inventory_mismatch",
            "The separately verified sealed inventory differs from the Attempt result.",
        )


def _rehash_artifact(cas: EvidenceCAS, artifact: OutputArtifact) -> None:
    try:
        cas.verify_exact_file_source(
            artifact.path,
            expected_sha256=artifact.sha256,
            expected_length=artifact.size_bytes,
        )
    except EvidenceStoreError as exc:
        code = (
            "sealed_output_changed"
            if exc.code
            in {
                "exact_file_changed",
                "exact_file_digest_mismatch",
                "exact_file_size_mismatch",
            }
            else "sealed_output_unavailable"
        )
        raise FormalAttemptBridgeError(
            code,
            "Fresh secure rehash differs from the exact Workstation output metadata.",
        ) from exc


def _unique_result_artifacts(
    result_artifacts: tuple[ResultArtifact, ...],
    *,
    accept_trusted: bool,
) -> tuple[tuple[OutputArtifact, bool], ...]:
    """Collapse only repeated origins while retaining content-identical aliases."""

    order: list[tuple[str, str]] = []
    by_origin: dict[tuple[str, str], tuple[OutputArtifact, bool]] = {}
    for item in result_artifacts:
        artifact = item.artifact
        declared_output = artifact.custody_root in {None, "output"} and not (
            artifact.custody_root == "output"
            and artifact.relative_path in _LEGACY_PROVIDER_OPERATIONAL_OUTPUT_PATHS
        )
        accepted = accept_trusted and not item.evidence_only and declared_output
        origin = _artifact_origin_key(artifact)
        previous = by_origin.get(origin)
        if previous is None:
            order.append(origin)
            by_origin[origin] = (artifact, accepted)
            continue
        previous_artifact, previous_accepted = previous
        if previous_artifact != artifact:
            raise FormalAttemptBridgeError(
                "sealed_output_origin_ambiguous",
                "One Workstation artifact origin resolves to conflicting sealed facts.",
            )
        by_origin[origin] = (
            previous_artifact,
            previous_accepted or accepted,
        )
    accepted = tuple(by_origin[key] for key in order if by_origin[key][1])
    evidence = tuple(by_origin[key] for key in order if not by_origin[key][1])
    return accepted + evidence


def _artifact_origin_key(artifact: OutputArtifact) -> tuple[str, str]:
    if artifact.custody_root is not None and artifact.relative_path is not None:
        return artifact.custody_root, artifact.relative_path
    return "legacy", artifact.name


def _artifact_logical_name(artifact: OutputArtifact) -> str:
    if artifact.custody_root is not None and artifact.relative_path is not None:
        return f"{artifact.custody_root}/{artifact.relative_path}"
    return artifact.name


def _capture_verified_outputs(
    store: Any,
    *,
    cas: EvidenceCAS,
    authority: DirectExecutiveEpochAuthority,
    binding: FormalSessionBinding,
    result: AttemptResult,
    accept_trusted: bool,
    lease: WriterLease,
    actor: str,
) -> tuple[RawCaptureRecord | None, bool]:
    unique = _unique_result_artifacts(
        result.artifacts,
        accept_trusted=accept_trusted,
    )
    if not unique:
        return None, False

    verified: list[tuple[OutputArtifact, bool, str]] = []
    quarantine_by_digest: dict[str, str] = {}
    for artifact, _accepted in unique:
        _rehash_artifact(cas, artifact)
        logical_name = _artifact_logical_name(artifact)
        quarantine_reason = cas.classify_exact_file_quarantine(
            artifact.path,
            expected_sha256=artifact.sha256,
            expected_length=artifact.size_bytes,
            original_name=logical_name,
            media_type=artifact.media_type,
        )
        if quarantine_reason is not None:
            quarantine_by_digest.setdefault(artifact.sha256, quarantine_reason)
        verified.append((artifact, _accepted, logical_name))

    file_inputs = tuple(
        RawCaptureArtifactFileInput(
            role="accepted_output" if accepted else "evidence_only",
            logical_name=logical_name,
            source_path=Path(artifact.path),
            sha256=artifact.sha256,
            byte_length=artifact.size_bytes,
            # The linked Workstation result owns declared media/encoding.
            # Physical CAS identity remains digest-only and neutral so the
            # same bytes cannot be reinterpreted by another filename alias.
            media_type="application/octet-stream",
            encoding=None,
            # Blob quarantine is a physical-byte fact. If any logical alias
            # requires quarantine, every descriptor sharing those bytes points
            # at that one conservatively classified CAS Blob.
            quarantine_reason=quarantine_by_digest.get(artifact.sha256),
        )
        for artifact, accepted, logical_name in verified
    )
    observation_material = {
        "domain": _OBSERVATION_DOMAIN,
        "attempt_id": result.attempt_id,
        "result_digest_sha256": result.result_digest,
    }
    observation_id = (
        "formal-attempt-result:"
        + hashlib.sha256(canonical_json_bytes(observation_material)).hexdigest()
    )
    capture = prepare_raw_capture(
        store,
        mission_id=authority.mission_id,
        executive_epoch_id=authority.executive_epoch_id,
        capture_kind="output",
        observation_id=observation_id,
        assignment_id=binding.session_id,
        provenance={
            "kind": "workstation_attempt_result",
            "attempt_id": result.attempt_id,
            "result_digest_sha256": result.result_digest,
            "session_id": result.session_id,
            "session_digest_sha256": result.session_digest,
        },
        completion={
            "attempt_state": result.attempt_state.value,
            "provider_effect_certainty": result.provider_effect_certainty.value,
        },
        artifacts=file_inputs,
        cas=cas,
    )
    commit_raw_capture(
        store,
        cas=cas,
        record=capture,
        lease=lease,
        actor=actor,
    )
    return capture, any(accepted for _artifact, accepted in unique)


def reconcile_and_settle_formal_attempt(
    store: Any,
    *,
    cas: EvidenceCAS,
    authority: DirectExecutiveEpochAuthority,
    adapter: ResearchAttemptAdapter,
    session_id: str,
    attempt_id: str,
    lease: WriterLease,
    actor: str,
) -> FormalAttemptReconciliation:
    """Reconcile, verify, preserve raw output, and settle only an eligible Session."""

    _require_adapter(adapter)
    binding, _genesis, current = _session_state(
        store,
        authority=authority,
        session_id=session_id,
    )
    prior = adapter.journal.get_attempt(attempt_id)
    _require_record_binding(prior, binding)
    record = adapter.reconcile(attempt_id)
    _require_record_binding(record, binding)
    result = adapter.verify_result(attempt_id)
    sealed = adapter.verify_sealed_artifacts(attempt_id)
    _require_exact_result(
        result=result,
        binding=binding,
        attempt_id=attempt_id,
        sealed_artifacts=sealed,
    )

    current_is_terminal = current.record.document["lifecycle"] == "terminal"
    current_binding = current.record.document["terminal_binding"]
    exact_terminal_replay = bool(
        current_is_terminal
        and current_binding is not None
        and current_binding["attempt_result_ref"]["attempt_id"] == result.attempt_id
        and current_binding["attempt_result_ref"]["result_digest_sha256"]
        == result.result_digest
    )
    effect_is_known = (
        result.provider_effect_certainty is not ProviderEffectCertainty.UNKNOWN
    )
    state_is_known_terminal = result.attempt_state in _KNOWN_TERMINAL_STATES
    settlement_candidate = (not current_is_terminal) or exact_terminal_replay
    accept_trusted = (
        settlement_candidate and effect_is_known and state_is_known_terminal
    )

    capture, has_accepted_output = _capture_verified_outputs(
        store,
        cas=cas,
        authority=authority,
        binding=binding,
        result=result,
        accept_trusted=accept_trusted,
        lease=lease,
        actor=actor,
    )

    # UNKNOWN, active work, and unverifiable effects remain factual custody only.
    # Known failed/cancelled/force-stopped Attempts likewise preserve their exact
    # result and raw output without closing the immutable Session: only an
    # explicit materially corrected successor may use that retryable lineage.
    # SUCCEEDED and nonretryable FENCED results close the Session. An exact
    # already-terminal historical result remains replayable through the same
    # idempotent terminal call, including a retryable state terminalized by an
    # earlier runtime, so this change never erases an existing terminal fact.
    has_any_output = bool(result.artifacts)
    terminal_state = result.attempt_state in _SESSION_TERMINAL_STATES
    retryable_terminal_state = result.attempt_state in _RETRYABLE_TERMINAL_STATES
    may_settle = bool(
        exact_terminal_replay
        or (
            not current_is_terminal
            and effect_is_known
            and terminal_state
            and (
                result.attempt_state is AttemptState.FENCED
                or not has_any_output
                or has_accepted_output
            )
        )
    )
    if retryable_terminal_state and not exact_terminal_replay:
        may_settle = False
    terminal_outcome = None
    if may_settle:
        verified_result = _issue_verified_formal_session_result(
            session_id=binding.session_id,
            attempt_id=result.attempt_id,
            attempt_state=result.attempt_state.value,
            result_digest_sha256=result.result_digest,
            raw_capture_id=None if capture is None else capture.capture_id,
            raw_capture_digest_sha256=(
                None if capture is None else capture.digest_sha256
            ),
        )
        terminal_outcome = terminate_formal_session(
            store,
            authority=authority,
            verified_result=verified_result,
            lease=lease,
            actor=actor,
        )

    return FormalAttemptReconciliation(
        attempt_result=result,
        raw_capture=capture,
        session_was_terminal=current_is_terminal,
        terminal_outcome=terminal_outcome,
    )


__all__ = [
    "FormalAttemptBridgeError",
    "FormalAttemptControl",
    "FormalAttemptReconciliation",
    "FormalAttemptRuntimeFacts",
    "build_formal_attempt_adapter",
    "dispatch_formal_attempt",
    "execute_formal_attempt_operation",
    "formal_session_binding",
    "installed_release_source_digest",
    "prepare_formal_attempt",
    "reconcile_and_settle_formal_attempt",
]
