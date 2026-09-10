"""CALL-E provider backed by the official `calle-ai` SDK.

This is the default for live calls. The hand-rolled HTTP client in
``client.py`` remains as a fallback and as a second opinion: both build
byte-identical requests, which is a useful property when the question is
"is our understanding of the API right, or just self-consistent?"

Using the SDK is not cosmetic. It means CALL-E's own client decides how
requests are framed and how failures are typed, so we inherit their
intent rather than re-deriving it from a spec document.

The interesting part is how little else changes. This class satisfies the
same :class:`~app.adapters.calle.client.CallProvider` protocol, returns
the same :class:`NormalizedCall`, and runs through the same
:func:`~app.adapters.calle.mapper.map_call`. The policy engine, the state
machine and the persistence layer do not know it exists -- which is what
the adapter boundary was for.

Note on webhook signatures: the SDK ships `webhooks.verify`, but its own
docstring says CALL-E "no longer sends timestamp or signature headers"
and marks it deprecated. There is therefore nothing to verify, and
Resolve-E deliberately does not pretend otherwise. Terminal events are
trusted only as a *notification*; the decision is always made from state
fetched back from CALL-E (``docs/architecture/high-level.md``).
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from app.adapters.calle.mapper import map_call
from app.domain.commands import CallHandle, CreateCallCommand
from app.domain.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimited,
    ProviderTransientError,
    ValidationError,
)
from app.domain.models import NormalizedCall, is_valid_e164

log = logging.getLogger(__name__)

#: Guard against following a broken cursor forever.
MAX_EVENT_PAGES = 20


class SdkCalleProvider:
    """Live CALL-E access through the official Python SDK."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.heycall-e.com",
        timeout: float = 30.0,
    ) -> None:
        if not api_key:
            raise ValidationError("CALLE_API_KEY is not configured")

        # Imported here rather than at module scope so the rest of the
        # application -- and the whole test suite -- runs without the SDK
        # installed.
        try:
            from calle import CalleClient
        except ImportError as exc:  # pragma: no cover - dependency is declared
            raise ValidationError(
                "the calle-ai package is not installed; "
                "install it or set CALLE_USE_SDK=false"
            ) from exc

        self._client = CalleClient(api_key=api_key, base_url=base_url, timeout=timeout)

    # ── CallProvider ─────────────────────────────────────────────────

    def create_call(self, command: CreateCallCommand) -> CallHandle:
        """Create one call task.

        The SDK retries nothing on our behalf, so the idempotency key
        matters exactly as much as it does on the raw client: the same
        logical batch must always present the same key.
        """
        for r in command.recipients:
            if not is_valid_e164(r.phone_e164):
                raise ValidationError(
                    f"recipient phone {r.phone_e164!r} is not valid E.164",
                    context={"exception_id": r.exception_id},
                )

        recipients: list[dict[str, Any]] = [
            {
                "phones": [r.phone_e164],
                **({"region": r.region} if r.region else {}),
                **({"locale": r.locale} if r.locale else {}),
            }
            for r in command.recipients
        ]

        with _translated_errors("create_call"):
            raw = self._client.calls.create(
                task=command.task,
                recipients=recipients,
                result_schema=command.result_schema,
                recipient_result_schema=command.recipient_result_schema,
                metadata=command.metadata or None,
                webhook_url=command.webhook_url or None,
                idempotency_key=command.idempotency_key,
            )

        call = map_call(raw)
        log.info(
            "calle.sdk create_call ok",
            extra={
                "provider_call_id": call.provider_call_id,
                "idempotency_key": command.idempotency_key,
                "recipient_count": len(command.recipients),
            },
        )
        return CallHandle(
            provider_call_id=call.provider_call_id,
            status=call.status,
            provider_recipient_ids=tuple(
                r.provider_recipient_id for r in call.recipients
            ),
            # The SDK does not surface the HTTP status, so a replayed
            # call cannot be distinguished here. Reported as not
            # deduplicated rather than guessed at; the duplicate-call
            # guarantee rests on the key, not on this flag.
            deduplicated=False,
        )

    def get_call(self, call_id: str) -> NormalizedCall:
        _assert_call_id(call_id)
        with _translated_errors("get_call"):
            return map_call(self._client.calls.get(call_id))

    def list_events(self, call_id: str) -> list[dict[str, Any]]:
        """Every developer event for a call, following pagination.

        The SDK exposes ``cursor``/``limit``; a single page would quietly
        truncate the audit trail on a long call.
        """
        _assert_call_id(call_id)
        events: list[dict[str, Any]] = []
        cursor: str | None = None

        with _translated_errors("list_events"):
            for _ in range(MAX_EVENT_PAGES):
                page = self._client.calls.list_events(call_id, cursor=cursor)
                data = page.get("data")
                if isinstance(data, list):
                    events.extend(e for e in data if isinstance(e, dict))
                cursor = page.get("next_cursor")
                if not cursor:
                    break
            else:
                log.warning(
                    "stopped paging call events at the page cap",
                    extra={"provider_call_id": call_id, "pages": MAX_EVENT_PAGES},
                )
        return events

    def close(self) -> None:
        self._client.close()


def _assert_call_id(call_id: str) -> None:
    if not call_id or not call_id.startswith("call_"):
        raise ValidationError(f"not a CALL-E call id: {call_id!r}")


class _translated_errors:  # noqa: N801 - used as a context manager
    """Map SDK exceptions onto the internal error taxonomy.

    Nothing above the adapter should ever see a ``Calle*`` exception:
    retry decisions are driven by :class:`~app.domain.enums.ErrorClass`,
    and a provider-specific type leaking upward is how that rule quietly
    stops being true.
    """

    def __init__(self, operation: str) -> None:
        self.operation = operation

    def __enter__(self) -> None:
        return None

    # Returns Literal[False] so the type checker knows this manager
    # never swallows an exception -- it only re-raises a translated one.
    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> Literal[False]:
        if exc is None:
            return False

        # CalleAPIError keeps `code`, `status_code` and `details`, but
        # NOT `message` -- it is passed to Exception.__init__, so str()
        # is the only way to read it.
        from calle.errors import (
            CalleAPIError,
            CalleAuthenticationError,
            CalleConnectionError,
            CalleRateLimitError,
            CalleTimeoutError,
        )

        if isinstance(exc, CalleAuthenticationError):
            raise ProviderAuthError(
                f"CALL-E rejected our credentials during {self.operation}: {exc}",
                status_code=exc.status_code,
                provider_code=exc.code,
            ) from exc

        if isinstance(exc, CalleRateLimitError):
            raise ProviderRateLimited(
                f"CALL-E rate limited {self.operation}: {exc}",
                status_code=exc.status_code,
                provider_code=exc.code,
            ) from exc

        if isinstance(exc, CalleTimeoutError | CalleConnectionError):
            # Safe to retry -- with the same idempotency key.
            raise ProviderTransientError(
                f"CALL-E {self.operation} did not complete: {exc}"
            ) from exc

        if isinstance(exc, CalleAPIError):
            if exc.status_code >= 500:
                raise ProviderTransientError(
                    f"CALL-E server error during {self.operation}: {exc}",
                    status_code=exc.status_code,
                    provider_code=exc.code,
                ) from exc
            raise ProviderError(
                f"CALL-E rejected {self.operation} ({exc.status_code}): {exc}",
                status_code=exc.status_code,
                provider_code=exc.code,
            ) from exc

        return False
