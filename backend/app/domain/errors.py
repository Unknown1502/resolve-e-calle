"""Typed errors carrying an :class:`ErrorClass`.

The master prompt forbids catching bare ``Exception`` and continuing into
a successful state. Every failure that crosses a boundary is therefore
raised as a :class:`ResolveEError` with an explicit class, and the only
code permitted to decide "is this retryable?" is
:data:`app.domain.enums.RETRYABLE_ERROR_CLASSES`.
"""

from __future__ import annotations

from typing import Any

from app.domain.enums import RETRYABLE_ERROR_CLASSES, ErrorClass


class ResolveEError(Exception):
    """Base error. Always carries a class; never carries a secret."""

    error_class: ErrorClass = ErrorClass.INTERNAL

    def __init__(
        self,
        message: str,
        *,
        error_class: ErrorClass | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        if error_class is not None:
            self.error_class = error_class
        self.context = context or {}

    @property
    def retryable(self) -> bool:
        return self.error_class in RETRYABLE_ERROR_CLASSES

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}({self.error_class}: {self.message})"


class ValidationError(ResolveEError):
    error_class = ErrorClass.VALIDATION


class PolicyViolation(ResolveEError):
    """A policy refused an action. Never retried -- policy is deterministic."""

    error_class = ErrorClass.POLICY


class InvalidTransition(ResolveEError):
    """Attempted a state transition the state machine does not permit."""

    error_class = ErrorClass.VALIDATION


class TerminalStateProtected(InvalidTransition):
    """A late result tried to move an already-terminal workflow.

    This is not a bug and not an error condition for the caller -- it is
    the system working. Callers catch it, record the result as historical
    evidence, and leave the state alone.
    """


class ConcurrencyConflict(ResolveEError):
    """Optimistic version check failed. Reload and re-evaluate."""

    error_class = ErrorClass.CONCURRENCY


class ProviderError(ResolveEError):
    """Base for anything the CALL-E adapter raises."""

    error_class = ErrorClass.PERMANENT_PROVIDER

    def __init__(
        self,
        message: str,
        *,
        error_class: ErrorClass | None = None,
        status_code: int | None = None,
        provider_code: str | None = None,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message, error_class=error_class, context=context)
        self.status_code = status_code
        #: Raw provider diagnostic. Persisted and displayed, never branched on.
        #: See ``docs/provider-truth.md`` §7.
        self.provider_code = provider_code


class ProviderAuthError(ProviderError):
    error_class = ErrorClass.AUTHENTICATION


class ProviderRateLimited(ProviderError):
    error_class = ErrorClass.RATE_LIMIT


class ProviderTransientError(ProviderError):
    """Timeout, 5xx, or connection failure. Safe to retry with the same key."""

    error_class = ErrorClass.TRANSIENT_PROVIDER


class StructuredResultError(ResolveEError):
    """CALL-E could not produce a schema-valid result, or it failed our re-check."""

    error_class = ErrorClass.STRUCTURED_RESULT
