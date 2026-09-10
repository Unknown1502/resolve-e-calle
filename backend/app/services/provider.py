"""Provider selection and the live-call safety interlock.

Two independent conditions must hold before a real phone rings:

1. ``CALLE_LIVE_CALLS=true`` *and* an API key is present
   (:attr:`Settings.live_calls_enabled`);
2. if an allowlist is configured, every number in the batch is on it.

Either failing sends the batch to the deterministic mock instead. The
interlock is here rather than in the client because it is a *policy*
about this deployment, not a detail of the HTTP call -- and because a
single place to look is worth more than a clever one.
"""

from __future__ import annotations

import logging

from app.adapters.calle.client import CalleClient, CallProvider
from app.adapters.calle.mock import MockCalleProvider
from app.config import Settings, get_settings
from app.domain.commands import CreateCallCommand
from app.domain.errors import PolicyViolation, ValidationError

log = logging.getLogger(__name__)

#: Process-wide mock, so state (idempotency, created calls) survives
#: across requests the way a real provider's would.
_mock = MockCalleProvider()


def get_mock_provider() -> MockCalleProvider:
    return _mock


def reset_mock_provider() -> None:
    """Test helper. Never called in production paths."""
    global _mock
    _mock = MockCalleProvider()


def assert_numbers_allowed(command: CreateCallCommand, settings: Settings) -> None:
    """Refuse a live dispatch to any number outside the allowlist."""
    allowed = settings.allowed_numbers
    if not allowed:
        return
    offenders = [r.phone_e164 for r in command.recipients if r.phone_e164 not in allowed]
    if offenders:
        raise PolicyViolation(
            f"{len(offenders)} recipient number(s) are not on CALLE_ALLOWED_NUMBERS",
            context={"count": len(offenders)},
        )


def select_provider(
    command: CreateCallCommand, settings: Settings | None = None
) -> tuple[CallProvider, bool]:
    """Choose the provider for this batch.

    Returns:
        ``(provider, is_live)``. ``is_live`` is persisted on the batch
        and shown in the UI, so a mocked call can never be mistaken for
        a real one in a demo.
    """
    settings = settings or get_settings()

    if not settings.live_calls_enabled:
        log.info(
            "provider.mock selected",
            extra={"reason": "live calls disabled", "recipients": len(command.recipients)},
        )
        return _mock, False

    try:
        assert_numbers_allowed(command, settings)
    except PolicyViolation as exc:
        # Fail closed onto the mock rather than raising: the workflow
        # still progresses and the refusal is auditable, but no
        # unauthorised number is ever dialled.
        log.warning("provider.mock selected", extra={"reason": str(exc)})
        return _mock, False

    return live_provider(settings), True


def live_provider(settings: Settings) -> CallProvider:
    """The transport for real calls.

    Defaults to the official SDK. If it is unavailable for any reason we
    fall back to the raw HTTP client rather than failing the dispatch --
    both send identical requests, so the fallback is a transport change
    and not a behaviour change.
    """
    if settings.calle_use_sdk:
        try:
            from app.adapters.calle.sdk_client import SdkCalleProvider

            return SdkCalleProvider(
                settings.calle_api_key,
                base_url=settings.calle_base_url,
                timeout=settings.calle_timeout_seconds,
            )
        except ValidationError as exc:
            log.warning(
                "official SDK unavailable, falling back to the HTTP client",
                extra={"reason": str(exc)},
            )

    return CalleClient(
        settings.calle_api_key,
        base_url=settings.calle_base_url,
        timeout=settings.calle_timeout_seconds,
    )
