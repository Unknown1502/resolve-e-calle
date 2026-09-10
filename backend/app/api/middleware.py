"""Request correlation.

Every log line emitted while handling a request carries the same
``request_id``, so a single operator action can be followed from the API
through the policy engine and into the provider adapter -- which is what
the master prompt's Observability section asks for.

An inbound ``X-Request-ID`` is honoured so a reverse proxy or a caller's
own trace id survives into our logs rather than being replaced.
"""

from __future__ import annotations

import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.requests import Request
from starlette.responses import Response

from app.logging_setup import new_request_id, reset_request_id, set_request_id

log = logging.getLogger(__name__)

#: Cap on a caller-supplied id, so a hostile header cannot bloat logs.
MAX_INBOUND_ID_LENGTH = 128


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        inbound = request.headers.get("X-Request-ID", "").strip()
        # Accept a caller's id, but never trust its size or contents.
        request_id = (
            inbound[:MAX_INBOUND_ID_LENGTH]
            if inbound and inbound.isprintable()
            else new_request_id()
        )
        token = set_request_id(request_id)
        started = time.perf_counter()

        try:
            response = await call_next(request)
        except Exception:
            # Logged with the id attached before it propagates, so a 500
            # can still be traced back to the request that caused it.
            log.exception(
                "request failed",
                extra={"method": request.method, "path": request.url.path},
            )
            raise
        finally:
            reset_request_id(token)

        elapsed_ms = (time.perf_counter() - started) * 1000
        response.headers["X-Request-ID"] = request_id

        # Health checks run every few seconds; logging each one buries
        # everything that matters.
        if request.url.path != "/health":
            log.info(
                "request",
                extra={
                    "request_id": request_id,
                    "method": request.method,
                    "path": request.url.path,
                    "status": response.status_code,
                    "duration_ms": round(elapsed_ms, 1),
                },
            )
        return response
