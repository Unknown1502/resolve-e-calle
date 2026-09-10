"""The worker process: three loops over one database.

* **outbox** -- drains side effects committed alongside state changes,
  so a decision and its consequence can never diverge (``spec.md`` §7);
* **scanner** -- finds overdue exceptions and batches them into a single
  call task;
* **reconciliation** -- repairs missed webhooks by asking CALL-E for
  authoritative state.

Redis is used for a distributed lock and a wake-up nudge, never as the
queue itself. The queue is the ``outbox`` table, because a Redis-backed
queue cannot be written atomically with the state change that produced
the message -- which is the exact split-brain the outbox pattern exists
to prevent.
"""

from __future__ import annotations

import logging
import signal
import threading
import time
from datetime import UTC, datetime

from app.config import get_settings
from app.db import repositories as repo
from app.db.session import session_scope
from app.domain.errors import ResolveEError
from app.logging_setup import configure_logging
from app.services.call_orchestrator import resolve_exceptions
from app.services.reconciliation_service import reconcile_call, sweep_stale_calls

log = logging.getLogger(__name__)

_stop = threading.Event()

#: Batch size for one scan. Bounded so a backlog cannot produce a call
#: task with an unreasonable number of recipients.
MAX_BATCH_RECIPIENTS = 5


def _now() -> datetime:
    return datetime.now(UTC)


def try_lock(name: str, ttl_seconds: int) -> bool:
    """Best-effort distributed lock so two workers do not duplicate work.

    Correctness does not depend on it: the outbox uses
    ``FOR UPDATE SKIP LOCKED`` and every side effect is idempotent. This
    only avoids wasted effort.
    """
    settings = get_settings()
    try:
        import redis

        client = redis.Redis.from_url(settings.redis_url)
        return bool(client.set(f"resolve-e:lock:{name}", "1", nx=True, ex=ttl_seconds))
    except Exception as exc:  # noqa: BLE001 - lock is advisory
        log.debug("redis lock unavailable, proceeding", extra={"lock": name, "error": str(exc)})
        return True


# ── loops ────────────────────────────────────────────────────────────


def drain_outbox() -> int:
    """Deliver pending side effects. Returns how many were handled."""
    handled = 0
    with session_scope() as session:
        for message in repo.claim_outbox(session, limit=10):
            try:
                if message.event_type == "reconcile_call":
                    reconcile_call(session, message.payload["provider_call_id"])
                else:
                    log.warning(
                        "unknown outbox event type", extra={"event_type": message.event_type}
                    )
                repo.mark_delivered(session, message)
                handled += 1
            except ResolveEError as exc:
                # Classified and retryable-aware. Never swallowed into
                # a delivered state.
                repo.mark_failed(
                    session,
                    message,
                    f"{exc.error_class}: {exc.message}",
                    retry_in_s=30 if exc.retryable else 300,
                )
                log.warning(
                    "outbox delivery failed",
                    extra={
                        "dedupe_key": message.dedupe_key,
                        "error_class": str(exc.error_class),
                        "retryable": exc.retryable,
                    },
                )
    return handled


def scan_exceptions() -> int:
    """Batch overdue exceptions into one call task."""
    if not try_lock("scanner", ttl_seconds=30):
        return 0

    with session_scope() as session:
        candidates = repo.find_callable_exceptions(
            session, limit=MAX_BATCH_RECIPIENTS, claim=True
        )
        if not candidates:
            return 0
        ids = [c.id for c in candidates]
        log.info("scanner batching exceptions", extra={"count": len(ids)})
        try:
            batch = resolve_exceptions(session, ids)
        except ResolveEError as exc:
            log.warning(
                "scanner dispatch failed",
                extra={"error_class": str(exc.error_class), "retryable": exc.retryable},
            )
            return 0
        return len(ids) if batch is not None else 0


def reconcile_stale() -> int:
    if not try_lock("reconciler", ttl_seconds=30):
        return 0
    with session_scope() as session:
        return len(sweep_stale_calls(session))


def _loop(name: str, fn, interval: int) -> None:
    while not _stop.is_set():
        try:
            if count := fn():
                log.info(f"{name} did work", extra={"count": count})
        except Exception:  # noqa: BLE001 - a loop must not die silently
            log.exception(f"{name} loop error")
        _stop.wait(interval)


def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    log.info("worker starting", extra={"live_calls": settings.live_calls_enabled})

    def handle_signal(signum, _frame):
        log.info("worker stopping", extra={"signal": signum})
        _stop.set()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    threads = [
        threading.Thread(
            target=_loop, args=("outbox", drain_outbox, settings.outbox_interval_seconds),
            daemon=True, name="outbox",
        ),
        threading.Thread(
            target=_loop, args=("scanner", scan_exceptions, settings.scanner_interval_seconds),
            daemon=True, name="scanner",
        ),
        threading.Thread(
            target=_loop,
            args=("reconciler", reconcile_stale, settings.reconciliation_interval_seconds),
            daemon=True, name="reconciler",
        ),
    ]
    for t in threads:
        t.start()

    while not _stop.is_set():
        time.sleep(0.5)
    for t in threads:
        t.join(timeout=5)
    log.info("worker stopped")


if __name__ == "__main__":
    main()
