"""Provider-agnostic commands.

The orchestrator speaks these; the adapter translates them into CALL-E
requests. Nothing here mentions CALL-E, which is what allows a
``GoalsCallProvider`` to be added later without touching the policy
engine (``docs/provider-truth.md`` §8).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from app.domain.models import region_and_locale_for

#: Provider cap on the Idempotency-Key header.
MAX_IDEMPOTENCY_KEY_LENGTH = 255


@dataclass(frozen=True, slots=True)
class CallRecipientCommand:
    """One supplier to reach inside a call task."""

    workflow_run_id: str
    exception_id: str
    attempt_no: int
    po_number: str
    supplier_name: str
    recipient_name: str | None
    phone_e164: str
    #: Inferred from the dial code when not given. Never defaulted to a
    #: country the number does not belong to -- the provider uses these
    #: for routing and compliance.
    region: str | None = None
    locale: str | None = None

    def __post_init__(self) -> None:
        if self.region is None or self.locale is None:
            region, locale = region_and_locale_for(self.phone_e164)
            # frozen dataclass: assign through object.__setattr__.
            if self.region is None:
                object.__setattr__(self, "region", region)
            if self.locale is None:
                object.__setattr__(self, "locale", locale)

    @property
    def attempt_key(self) -> str:
        """This recipient's contribution to the batch idempotency key."""
        return f"{self.workflow_run_id}:{self.attempt_no}"


@dataclass(frozen=True, slots=True)
class CreateCallCommand:
    """A request to place one call task covering one or more recipients."""

    recipients: tuple[CallRecipientCommand, ...]
    task: str
    result_schema: dict[str, Any]
    recipient_result_schema: dict[str, Any]
    idempotency_key: str
    webhook_url: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.recipients:
            raise ValueError("a call command needs at least one recipient")
        if len(self.idempotency_key) > MAX_IDEMPOTENCY_KEY_LENGTH:
            raise ValueError(
                f"idempotency key exceeds {MAX_IDEMPOTENCY_KEY_LENGTH} characters"
            )


@dataclass(frozen=True, slots=True)
class CallHandle:
    """What the provider gave back when a call task was accepted."""

    provider_call_id: str
    status: str
    #: Provider-assigned recipient ids, in the order they were submitted.
    #: Correlating on submission order is the only option because the
    #: create response is the first place these ids exist.
    provider_recipient_ids: tuple[str, ...] = ()
    #: True when the provider returned an existing call for a reused
    #: idempotency key rather than creating a new one.
    deduplicated: bool = False


def compute_batch_idempotency_key(
    recipients: tuple[CallRecipientCommand, ...],
) -> str:
    """Derive a stable key for one logical batch.

    The key is content-derived from the sorted set of
    ``(workflow_run_id, attempt_no)`` pairs, so that:

    * retrying the *same* logical batch after a timeout produces a
      byte-identical key and the provider returns the original call
      rather than dialling anyone twice;
    * a batch with different membership produces a different key, so two
      genuinely different batches never collide.

    Ordering of the input does not affect the key. Length is bounded at
    46 characters, well inside the provider's 255 limit.

    See ``docs/provider-truth.md`` §9.
    """
    if not recipients:
        raise ValueError("cannot derive an idempotency key for an empty batch")
    parts = sorted(r.attempt_key for r in recipients)
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:32]
    return f"resolve-e:batch:{digest}"
