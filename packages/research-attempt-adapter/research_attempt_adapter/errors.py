"""Stable package errors with content-safe diagnostic codes."""

from __future__ import annotations


class ResearchAttemptAdapterError(RuntimeError):
    """Base error whose code is safe to persist and expose."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ContractError(ResearchAttemptAdapterError):
    """An input failed the package's strict internal contract."""


class JournalConflictError(ResearchAttemptAdapterError):
    """Stable identity was reused with different content."""


class AttemptBlockedError(ResearchAttemptAdapterError):
    """The requested transition is deliberately unavailable."""


class ProviderRejectedError(ResearchAttemptAdapterError):
    """The provider proved that no external effect began."""


class ProviderEffectUnknownError(ResearchAttemptAdapterError):
    """The provider cannot prove whether an external effect occurred."""


class SourceVerificationError(ResearchAttemptAdapterError):
    """The exact launch worktree could not be bound to the frozen source."""


class InjectedCrash(BaseException):
    """Test-only process-crash analogue that bypasses normal error handling."""
