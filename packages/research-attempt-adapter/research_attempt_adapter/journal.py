"""Append-only SQLite journal for formal research provider effects."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .errors import ContractError, JournalConflictError
from .models import (
    AttemptEvent,
    AttemptIntent,
    AttemptRecord,
    AttemptState,
    ExecutionFence,
    FormalSessionBinding,
    OutputArtifact,
    ProviderEffectCertainty,
    ProviderState,
    StagedInputAttachment,
    canonical_json_bytes,
)


SCHEMA_VERSION = 8


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class AttemptJournal:
    """One immutable intent row plus one append-only event stream per Attempt."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(str(self.path))
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='journal_meta'"
            ).fetchone()
            if exists:
                row = connection.execute(
                    "SELECT schema_version FROM journal_meta"
                ).fetchone()
                if row is None or int(row["schema_version"]) != SCHEMA_VERSION:
                    raise ContractError(
                        "unsupported_journal_schema",
                        "Historical journal schemas are inert and cannot launch through this adapter.",
                    )
                return
            connection.executescript(
                """
                CREATE TABLE journal_meta(
                    schema_version INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE sessions(
                    session_id TEXT PRIMARY KEY,
                    session_digest TEXT NOT NULL,
                    session_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE fences(
                    fence_id TEXT PRIMARY KEY,
                    fence_digest TEXT NOT NULL,
                    fence_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE fence_revocations(
                    fence_id TEXT PRIMARY KEY REFERENCES fences(fence_id),
                    operation_key TEXT NOT NULL UNIQUE,
                    recorded_at TEXT NOT NULL
                );
                CREATE TABLE attempts(
                    attempt_id TEXT PRIMARY KEY,
                    session_id TEXT NOT NULL REFERENCES sessions(session_id),
                    intent_digest TEXT NOT NULL,
                    intent_json TEXT NOT NULL,
                    fence_id TEXT NOT NULL REFERENCES fences(fence_id),
                    previous_attempt_id TEXT REFERENCES attempts(attempt_id),
                    input_staging_root TEXT NOT NULL,
                    bootstrap_manifest_path TEXT NOT NULL,
                    bootstrap_manifest_sha256 TEXT NOT NULL,
                    staged_attachments_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE attempt_events(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    attempt_id TEXT NOT NULL REFERENCES attempts(attempt_id),
                    operation_key TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    state TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    provider_ref TEXT,
                    provider_state TEXT,
                    detail_code TEXT,
                    evidence_only INTEGER NOT NULL CHECK(evidence_only IN (0,1)),
                    effect_certainty TEXT NOT NULL,
                    exit_code INTEGER,
                    artifacts_json TEXT NOT NULL,
                    resource_facts_json TEXT NOT NULL
                );
                CREATE INDEX attempt_events_attempt_sequence
                    ON attempt_events(attempt_id, sequence);
                """
            )
            connection.execute(
                "INSERT INTO journal_meta(schema_version, created_at) VALUES (?, ?)",
                (SCHEMA_VERSION, _now()),
            )
            for table in (
                "journal_meta",
                "sessions",
                "fences",
                "fence_revocations",
                "attempts",
                "attempt_events",
            ):
                connection.executescript(
                    f"""
                    CREATE TRIGGER {table}_no_update
                    BEFORE UPDATE ON {table}
                    BEGIN SELECT RAISE(ABORT, 'append_only'); END;
                    CREATE TRIGGER {table}_no_delete
                    BEFORE DELETE ON {table}
                    BEGIN SELECT RAISE(ABORT, 'append_only'); END;
                    """
                )

    def install_fence(self, fence: ExecutionFence) -> ExecutionFence:
        if type(fence) is not ExecutionFence:
            raise ContractError("invalid_fence", "fence must be typed.")
        payload = canonical_json_bytes(fence.__dict__).decode("utf-8")
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT fence_digest, fence_json FROM fences WHERE fence_id=?",
                (fence.fence_id,),
            ).fetchone()
            if row:
                if row["fence_digest"] != fence.digest or row["fence_json"] != payload:
                    raise JournalConflictError(
                        "fence_identity_conflict", "Fence identity was reused with other content."
                    )
                return fence
            connection.execute(
                "INSERT INTO fences VALUES (?, ?, ?, ?)",
                (fence.fence_id, fence.digest, payload, _now()),
            )
        return fence

    def get_fence(self, fence_id: str) -> ExecutionFence:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT fence_json FROM fences WHERE fence_id=?", (fence_id,)
            ).fetchone()
        if row is None:
            raise ContractError("fence_not_found", "The execution fence does not exist.")
        return ExecutionFence.from_dict(json.loads(row["fence_json"]))

    def is_fence_active(self, fence_id: str) -> bool:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """SELECT f.fence_id, r.fence_id AS revoked
                   FROM fences f LEFT JOIN fence_revocations r USING(fence_id)
                   WHERE f.fence_id=?""",
                (fence_id,),
            ).fetchone()
        return row is not None and row["revoked"] is None

    def revoke_fence(self, fence_id: str) -> bool:
        self.get_fence(fence_id)
        operation_key = f"revoke:{fence_id}"
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT 1 FROM fence_revocations WHERE fence_id=?", (fence_id,)
            ).fetchone()
            if row:
                return False
            connection.execute(
                "INSERT INTO fence_revocations VALUES (?, ?, ?)",
                (fence_id, operation_key, _now()),
            )
        return True

    def create_attempt(
        self,
        session: FormalSessionBinding,
        intent: AttemptIntent,
        *,
        fence_id: str,
        input_staging_root: str,
        bootstrap_manifest_path: str,
        bootstrap_manifest_sha256: str,
        staged_attachments: tuple[StagedInputAttachment, ...],
    ) -> AttemptRecord:
        if session.session_id != intent.session_id or session.digest != intent.session_digest:
            raise ContractError(
                "session_attempt_mismatch", "Attempt does not bind the exact formal Session."
            )
        session_json = canonical_json_bytes(session.as_dict()).decode("utf-8")
        intent_json = canonical_json_bytes(intent.as_dict()).decode("utf-8")
        staged_json = canonical_json_bytes(
            [item.as_dict() for item in staged_attachments]
        ).decode("utf-8")
        created_at = _now()
        with closing(self._connect()) as connection, connection:
            existing_session = connection.execute(
                "SELECT session_digest, session_json FROM sessions WHERE session_id=?",
                (session.session_id,),
            ).fetchone()
            if existing_session and (
                existing_session["session_digest"] != session.digest
                or existing_session["session_json"] != session_json
            ):
                raise JournalConflictError(
                    "session_identity_conflict", "Session identity was reused with other meaning."
                )
            if not existing_session:
                connection.execute(
                    "INSERT INTO sessions VALUES (?, ?, ?, ?)",
                    (session.session_id, session.digest, session_json, created_at),
                )
            row = connection.execute(
                "SELECT * FROM attempts WHERE attempt_id=?", (intent.attempt_id,)
            ).fetchone()
            expected = (
                session.session_id,
                intent.digest,
                intent_json,
                fence_id,
                intent.previous_attempt_id,
                input_staging_root,
                bootstrap_manifest_path,
                bootstrap_manifest_sha256,
                staged_json,
            )
            if row:
                actual = tuple(
                    row[name]
                    for name in (
                        "session_id",
                        "intent_digest",
                        "intent_json",
                        "fence_id",
                        "previous_attempt_id",
                        "input_staging_root",
                        "bootstrap_manifest_path",
                        "bootstrap_manifest_sha256",
                        "staged_attachments_json",
                    )
                )
                if actual != expected:
                    raise JournalConflictError(
                        "attempt_identity_conflict",
                        "Attempt identity was reused with different operational content.",
                    )
                return self.get_attempt(intent.attempt_id)
            connection.execute(
                """INSERT INTO attempts(
                    attempt_id, session_id, intent_digest, intent_json, fence_id,
                    previous_attempt_id, input_staging_root, bootstrap_manifest_path,
                    bootstrap_manifest_sha256, staged_attachments_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (intent.attempt_id, *expected, created_at),
            )
            self._append_event_connection(
                connection,
                attempt_id=intent.attempt_id,
                operation_key=f"prepare:{intent.attempt_id}",
                event_type="prepared",
                state=AttemptState.PREPARED,
                effect_certainty=ProviderEffectCertainty.NONE,
            )
        return self.get_attempt(intent.attempt_id)

    def append_event(
        self,
        *,
        attempt_id: str,
        operation_key: str,
        event_type: str,
        state: AttemptState,
        provider_ref: str | None = None,
        provider_state: ProviderState | None = None,
        detail_code: str | None = None,
        evidence_only: bool = False,
        effect_certainty: ProviderEffectCertainty = ProviderEffectCertainty.NONE,
        exit_code: int | None = None,
        artifacts: tuple[OutputArtifact, ...] = (),
        resource_facts: dict[str, object] | None = None,
    ) -> AttemptEvent:
        with closing(self._connect()) as connection, connection:
            return self._append_event_connection(
                connection,
                attempt_id=attempt_id,
                operation_key=operation_key,
                event_type=event_type,
                state=state,
                provider_ref=provider_ref,
                provider_state=provider_state,
                detail_code=detail_code,
                evidence_only=evidence_only,
                effect_certainty=effect_certainty,
                exit_code=exit_code,
                artifacts=artifacts,
                resource_facts=resource_facts,
            )

    def _append_event_connection(
        self,
        connection: sqlite3.Connection,
        *,
        attempt_id: str,
        operation_key: str,
        event_type: str,
        state: AttemptState,
        provider_ref: str | None = None,
        provider_state: ProviderState | None = None,
        detail_code: str | None = None,
        evidence_only: bool = False,
        effect_certainty: ProviderEffectCertainty = ProviderEffectCertainty.NONE,
        exit_code: int | None = None,
        artifacts: tuple[OutputArtifact, ...] = (),
        resource_facts: dict[str, object] | None = None,
    ) -> AttemptEvent:
        existing = connection.execute(
            "SELECT * FROM attempt_events WHERE operation_key=?", (operation_key,)
        ).fetchone()
        if existing:
            event = self._event_from_row(existing)
            comparable = (
                attempt_id,
                event_type,
                AttemptState(state),
                provider_ref,
                None if provider_state is None else ProviderState(provider_state),
                detail_code,
                evidence_only,
                ProviderEffectCertainty(effect_certainty),
                exit_code,
                tuple(artifacts),
                dict(resource_facts or {}),
            )
            actual = (
                event.attempt_id,
                event.event_type,
                event.state,
                event.provider_ref,
                event.provider_state,
                event.detail_code,
                event.evidence_only,
                event.effect_certainty,
                event.exit_code,
                event.artifacts,
                dict(event.resource_facts),
            )
            if actual != comparable:
                raise JournalConflictError(
                    "operation_identity_conflict",
                    "Operation identity was reused for another journal fact.",
                )
            return event
        if connection.execute(
            "SELECT 1 FROM attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone() is None:
            raise ContractError("attempt_not_found", "Attempt does not exist.")
        recorded_at = _now()
        artifacts_json = canonical_json_bytes(
            [item.as_dict() for item in artifacts]
        ).decode("utf-8")
        resources_json = canonical_json_bytes(resource_facts or {}).decode("utf-8")
        cursor = connection.execute(
            """INSERT INTO attempt_events(
                attempt_id, operation_key, event_type, state, recorded_at,
                provider_ref, provider_state, detail_code, evidence_only,
                effect_certainty, exit_code, artifacts_json, resource_facts_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                attempt_id,
                operation_key,
                event_type,
                AttemptState(state).value,
                recorded_at,
                provider_ref,
                None if provider_state is None else ProviderState(provider_state).value,
                detail_code,
                int(evidence_only),
                ProviderEffectCertainty(effect_certainty).value,
                exit_code,
                artifacts_json,
                resources_json,
            ),
        )
        row = connection.execute(
            "SELECT * FROM attempt_events WHERE sequence=?", (cursor.lastrowid,)
        ).fetchone()
        assert row is not None
        return self._event_from_row(row)

    def event_for_operation(self, operation_key: str) -> AttemptEvent | None:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                "SELECT * FROM attempt_events WHERE operation_key=?", (operation_key,)
            ).fetchone()
        return None if row is None else self._event_from_row(row)

    def list_events(self, attempt_id: str) -> tuple[AttemptEvent, ...]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                "SELECT * FROM attempt_events WHERE attempt_id=? ORDER BY sequence",
                (attempt_id,),
            ).fetchall()
        return tuple(self._event_from_row(row) for row in rows)

    def get_attempt(self, attempt_id: str) -> AttemptRecord:
        with closing(self._connect()) as connection, connection:
            row = connection.execute(
                """SELECT a.*, s.session_json FROM attempts a
                   JOIN sessions s USING(session_id) WHERE a.attempt_id=?""",
                (attempt_id,),
            ).fetchone()
        if row is None:
            raise ContractError("attempt_not_found", "Attempt does not exist.")
        events = self.list_events(attempt_id)
        if not events:
            raise JournalConflictError("attempt_without_events", "Attempt has no journal history.")
        trusted = tuple(event for event in events if not event.evidence_only)
        head = trusted[-1]
        provider_ref = next(
            (event.provider_ref for event in reversed(trusted) if event.provider_ref),
            None,
        )
        staged = tuple(
            StagedInputAttachment.from_dict(item)
            for item in json.loads(row["staged_attachments_json"])
        )
        return AttemptRecord(
            session=FormalSessionBinding.from_dict(json.loads(row["session_json"])),
            intent=AttemptIntent.from_dict(json.loads(row["intent_json"])),
            fence_id=row["fence_id"],
            state=head.state,
            provider_ref=provider_ref,
            created_at=row["created_at"],
            last_event_at=events[-1].recorded_at,
            event_count=len(events),
            input_staging_root=row["input_staging_root"],
            bootstrap_manifest_path=row["bootstrap_manifest_path"],
            bootstrap_manifest_sha256=row["bootstrap_manifest_sha256"],
            staged_attachments=staged,
        )

    def list_attempts(self) -> tuple[AttemptRecord, ...]:
        with closing(self._connect()) as connection, connection:
            ids = [
                row["attempt_id"]
                for row in connection.execute(
                    "SELECT attempt_id FROM attempts ORDER BY created_at, attempt_id"
                ).fetchall()
            ]
        return tuple(self.get_attempt(attempt_id) for attempt_id in ids)

    def attempts_for_fence(self, fence_id: str) -> tuple[AttemptRecord, ...]:
        return tuple(
            record for record in self.list_attempts() if record.fence_id == fence_id
        )

    def requiring_reconciliation(self) -> tuple[AttemptRecord, ...]:
        states = {
            AttemptState.LAUNCH_REQUESTED,
            AttemptState.RUNNING,
            AttemptState.CANCEL_REQUESTED,
            AttemptState.FORCE_STOP_REQUESTED,
            AttemptState.UNKNOWN,
        }
        return tuple(record for record in self.list_attempts() if record.state in states)

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> AttemptEvent:
        return AttemptEvent(
            sequence=int(row["sequence"]),
            attempt_id=row["attempt_id"],
            operation_key=row["operation_key"],
            event_type=row["event_type"],
            state=AttemptState(row["state"]),
            recorded_at=row["recorded_at"],
            provider_ref=row["provider_ref"],
            provider_state=(
                None if row["provider_state"] is None else ProviderState(row["provider_state"])
            ),
            detail_code=row["detail_code"],
            evidence_only=bool(row["evidence_only"]),
            effect_certainty=ProviderEffectCertainty(row["effect_certainty"]),
            exit_code=row["exit_code"],
            artifacts=tuple(
                OutputArtifact.from_dict(item) for item in json.loads(row["artifacts_json"])
            ),
            resource_facts=json.loads(row["resource_facts_json"]),
        )


def append_only_tables() -> Iterable[str]:
    return (
        "journal_meta",
        "sessions",
        "fences",
        "fence_revocations",
        "attempts",
        "attempt_events",
    )
