"""CALL-E HTTP client.

The only module in the application that performs network I/O against the
provider. It owns three responsibilities and nothing else:

* build valid requests (``docs/provider-truth.md`` §2);
* retry *safely* -- always with the same ``Idempotency-Key``, so a
  create that timed out after reaching CALL-E returns the original call
  instead of dialling anyone twice;
* map every HTTP failure into the internal error taxonomy, so no
  ``httpx`` exception escapes into business code.

A timeout during create is the dangerous case. The request may have
reached the provider and started a phone call. Retrying with a *new* key
would place a second call to a real person. So the key is computed once,
by the orchestrator, and reused for every attempt at the same logical
create (``reliability-and-failure.md`` §4).
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

import httpx

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

DEFAULT_BASE_URL = "https://api.heycall-e.com"
#: Generous enough for a create to be accepted, short enough that a
#: hung socket does not stall the worker.
DEFAULT_TIMEOUT_SECONDS = 30.0
MAX_CREATE_ATTEMPTS = 3
#: Guard against following a broken cursor forever.
MAX_EVENT_PAGES = 20


@runtime_checkable
class CallProvider(Protocol):
    """The contract from ``docs/api-contract.md``.

    Three implementations satisfy it: the official-SDK provider, this
    HTTP client, and the deterministic mock. That is what lets every test
    above the adapter run without a network, and what let the transport
    switch to the SDK without touching a line of policy.

    Runtime-checkable so conformance can be asserted rather than assumed.
    """

    def create_call(self, command: CreateCallCommand) -> CallHandle: ...

    def get_call(self, call_id: str) -> NormalizedCall: ...

    def list_events(self, call_id: str) -> list[dict[str, Any]]: ...


class CalleClient:
    """Live CALL-E Developer API client (`/v1/calls`)."""

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        client: httpx.Client | None = None,
    ) -> None:
        if not api_key:
            raise ValidationError("CALLE_API_KEY is not configured")
        self._base_url = base_url.rstrip("/")
        self._client = client or httpx.Client(base_url=self._base_url, timeout=timeout)
        # Applied whether the client was injected or built here. An
        # injected transport is a test seam, not a reason to send
        # unauthenticated requests -- and a client that authenticates
        # only on one construction path is a trap.
        self._client.headers.update(
            {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                # No URL: awesome-phone-call-agents is where the skill is
                # submitted, not where this application lives, and a
                # User-Agent that claims otherwise is just wrong.
                "User-Agent": "resolve-e/0.1",
            }
        )

    # ── public API ────────────────────────────────────────────────────

    def create_call(self, command: CreateCallCommand) -> CallHandle:
        """Create one call task, retrying transient failures safely.

        The ``Idempotency-Key`` header is identical across all attempts,
        so a retry after a timeout returns the original call.
        """
        for r in command.recipients:
            if not is_valid_e164(r.phone_e164):
                # Refused here rather than by the provider, so the
                # failure is ours and carries an auditable reason.
                raise ValidationError(
                    f"recipient phone {r.phone_e164!r} is not valid E.164",
                    context={"exception_id": r.exception_id},
                )

        payload = self._build_create_payload(command)
        headers = {"Idempotency-Key": command.idempotency_key}

        last_error: ProviderError | None = None
        for attempt in range(1, MAX_CREATE_ATTEMPTS + 1):
            try:
                response = self._client.post("/v1/calls", json=payload, headers=headers)
                raw = self._unwrap(response)
                call = map_call(raw)
                log.info(
                    "calle.create_call ok",
                    extra={
                        "provider_call_id": call.provider_call_id,
                        "idempotency_key": command.idempotency_key,
                        "recipient_count": len(command.recipients),
                        "http_attempt": attempt,
                    },
                )
                return CallHandle(
                    provider_call_id=call.provider_call_id,
                    status=call.status,
                    provider_recipient_ids=tuple(
                        r.provider_recipient_id for r in call.recipients
                    ),
                    # A 200 on a POST that would otherwise 201 is the
                    # provider replaying an existing call for this key.
                    deduplicated=response.status_code == 200,
                )
            except (ProviderTransientError, ProviderRateLimited) as exc:
                last_error = exc
                if attempt == MAX_CREATE_ATTEMPTS:
                    break
                log.warning(
                    "calle.create_call retrying with same idempotency key",
                    extra={
                        "idempotency_key": command.idempotency_key,
                        "http_attempt": attempt,
                        "error_class": str(exc.error_class),
                    },
                )

        assert last_error is not None
        raise last_error

    def get_call(self, call_id: str) -> NormalizedCall:
        """Fetch authoritative call state. The reconciliation path."""
        self._assert_call_id(call_id)
        response = self._request("GET", f"/v1/calls/{call_id}")
        return map_call(self._unwrap(response))

    def list_events(self, call_id: str) -> list[dict[str, Any]]:
        """Every developer event for a call, following pagination.

        A single page would quietly truncate the audit trail on a long
        call, and would also make this transport disagree with the SDK
        one -- which defeats the point of keeping both.
        """
        self._assert_call_id(call_id)
        events: list[dict[str, Any]] = []
        cursor: str | None = None

        for _ in range(MAX_EVENT_PAGES):
            params = {"cursor": cursor} if cursor else None
            response = self._request("GET", f"/v1/calls/{call_id}/events", params=params)
            body = self._unwrap(response)
            data = body.get("data")
            if isinstance(data, list):
                events.extend(e for e in data if isinstance(e, dict))
            cursor = body.get("next_cursor")
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

    # ── internals ─────────────────────────────────────────────────────

    @staticmethod
    def _assert_call_id(call_id: str) -> None:
        # Provider pattern: ^call_[A-Za-z0-9_-]+$
        if not call_id or not call_id.startswith("call_"):
            raise ValidationError(f"not a CALL-E call id: {call_id!r}")

    @staticmethod
    def _build_create_payload(command: CreateCallCommand) -> dict[str, Any]:
        """Build a ``CreateCallRequest``.

        ``additionalProperties: false`` on the provider schema means an
        unexpected key is a 400, so only documented fields are sent.
        """
        payload: dict[str, Any] = {
            "task": command.task,
            "recipients": [
                {
                    "phones": [r.phone_e164],
                    **({"region": r.region} if r.region else {}),
                    **({"locale": r.locale} if r.locale else {}),
                }
                for r in command.recipients
            ],
            "result_schema": command.result_schema,
            "recipient_result_schema": command.recipient_result_schema,
            "metadata": command.metadata,
        }
        if command.webhook_url:
            payload["webhook_url"] = command.webhook_url
        return payload

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return self._client.request(method, path, **kwargs)
        except httpx.TimeoutException as exc:
            raise ProviderTransientError(
                f"CALL-E request timed out: {method} {path}", context={"path": path}
            ) from exc
        except httpx.TransportError as exc:
            raise ProviderTransientError(
                f"CALL-E transport error: {exc}", context={"path": path}
            ) from exc

    def _unwrap(self, response: httpx.Response) -> dict[str, Any]:
        """Return the JSON body, or raise a classified provider error."""
        status = response.status_code

        if 200 <= status < 300:
            try:
                body = response.json()
            except ValueError as exc:
                raise ProviderError(
                    "CALL-E returned a non-JSON success body", status_code=status
                ) from exc
            if not isinstance(body, dict):
                raise ProviderError(
                    f"CALL-E returned {type(body).__name__}, expected an object",
                    status_code=status,
                )
            return body

        code, message = self._error_details(response)

        if status in (401, 403):
            raise ProviderAuthError(
                f"CALL-E rejected our credentials ({status}): {message}",
                status_code=status,
                provider_code=code,
            )
        if status == 429:
            raise ProviderRateLimited(
                f"CALL-E rate limited the request: {message}",
                status_code=status,
                provider_code=code,
            )
        if status == 409:
            # Idempotency conflict: the same key with different content.
            # A bug in key derivation, never something to retry.
            raise ProviderError(
                f"CALL-E idempotency conflict: {message}",
                status_code=status,
                provider_code=code,
            )
        if 400 <= status < 500:
            raise ProviderError(
                f"CALL-E rejected the request ({status}): {message}",
                status_code=status,
                provider_code=code,
            )

        raise ProviderTransientError(
            f"CALL-E server error ({status}): {message}",
            status_code=status,
            provider_code=code,
        )

    @staticmethod
    def _error_details(response: httpx.Response) -> tuple[str | None, str]:
        """Pull ``ErrorEnvelope`` fields without assuming they are present."""
        try:
            body = response.json()
        except ValueError:
            return None, response.text[:200]
        if isinstance(body, dict):
            err = body.get("error")
            if isinstance(err, dict):
                return err.get("code"), str(err.get("message") or "")[:500]
            return body.get("code"), str(body.get("message") or "")[:500]
        return None, str(body)[:200]
