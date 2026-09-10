"""HTTP contract tests.

Everything below goes through the real ASGI app, so these cover what the
browser and CALL-E actually see: status codes, validation, the webhook
handshake, and -- most importantly -- that no secret is ever serialised
into a response.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tests.conftest import requires_postgres

pytestmark = [requires_postgres, pytest.mark.integration]


@pytest.fixture
def client(db: Session) -> TestClient:
    """A TestClient bound to the per-test database session."""
    from app.db.session import get_db
    from app.main import app

    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def make_exception(client: TestClient, po: str = "PO-9001", **over) -> str:
    payload = {
        "po_number": po,
        "supplier_name": "Acme Components",
        "recipient_name": "Jordan",
        "recipient_phone_e164": "+15550001111",
        "ack_due_at": (datetime.now(UTC) - timedelta(hours=26)).isoformat(),
        **over,
    }
    response = client.post("/api/exceptions", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["exception_id"]


class TestHealth:
    def test_reports_ok_with_a_reachable_database(self, client: TestClient) -> None:
        body = client.get("/health").json()
        assert body["status"] == "ok"
        assert body["database"] == "ok"

    def test_never_returns_the_api_key(self, client: TestClient) -> None:
        # The health endpoint reports whether a key is present, never
        # the key itself. docs/security-privacy.md.
        raw = client.get("/health").text
        assert "calle_api_key_present" in raw
        assert "iams_" not in raw
        config = client.get("/health").json()["config"]
        assert "calle_api_key" not in config
        assert isinstance(config["calle_api_key_present"], bool)

    def test_reports_whether_live_calling_is_armed(self, client: TestClient) -> None:
        assert client.get("/health").json()["config"]["live_calls_enabled"] is False


class TestCreateException:
    def test_creates_and_returns_open(self, client: TestClient) -> None:
        response = client.post(
            "/api/exceptions",
            json={
                "po_number": "PO-9100",
                "supplier_name": "Globex",
                "recipient_phone_e164": "+15550002222",
                "ack_due_at": datetime.now(UTC).isoformat(),
            },
        )
        assert response.status_code == 201
        assert response.json()["state"] == "OPEN"

    @pytest.mark.parametrize(
        "phone",
        ["5550001111", "+1 555 000 1111", "555-0100", "", "+0123456", "not-a-number"],
    )
    def test_rejects_any_phone_that_is_not_e164(
        self, client: TestClient, phone: str
    ) -> None:
        # Reliability rule 15: no arbitrary phone targets from the UI.
        response = client.post(
            "/api/exceptions",
            json={
                "po_number": "PO-9101",
                "supplier_name": "Globex",
                "recipient_phone_e164": phone,
                "ack_due_at": datetime.now(UTC).isoformat(),
            },
        )
        assert response.status_code == 422

    def test_duplicate_po_number_is_a_conflict(self, client: TestClient) -> None:
        make_exception(client, "PO-9102")
        response = client.post(
            "/api/exceptions",
            json={
                "po_number": "PO-9102",
                "supplier_name": "Globex",
                "recipient_phone_e164": "+15550002222",
                "ack_due_at": datetime.now(UTC).isoformat(),
            },
        )
        assert response.status_code == 409


class TestQueueAndDetail:
    def test_list_is_empty_before_anything_exists(self, client: TestClient) -> None:
        assert client.get("/api/exceptions").json() == []

    def test_detail_masks_the_phone_number(self, client: TestClient) -> None:
        exc = make_exception(client, "PO-9200")
        detail = client.get(f"/api/exceptions/{exc}").json()
        # Enough to recognise, not enough to redistribute.
        assert detail["recipient_phone_masked"] != "+15550001111"
        assert "+15550001111" not in client.get(f"/api/exceptions/{exc}").text

    def test_unknown_exception_is_404(self, client: TestClient) -> None:
        assert client.get("/api/exceptions/does-not-exist").status_code == 404

    def test_detail_carries_the_whole_evidence_chain(self, client: TestClient) -> None:
        exc = make_exception(client, "PO-4821")
        client.post("/api/exceptions/resolve", json={"exception_ids": [exc]})
        detail = client.get(f"/api/exceptions/{exc}").json()

        assert detail["attempts"], "an attempt should be recorded"
        assert detail["batches"], "a call batch should be recorded"
        assert detail["decisions"], "policy decisions should be recorded"
        assert detail["audit"], "the audit trail should be populated"
        assert detail["batches"][-1]["provider_call_id"].startswith("call_")
        assert detail["batches"][-1]["idempotency_key"].startswith("resolve-e:batch:")


class TestResolve:
    def test_resolving_dispatches_one_call_for_many_exceptions(
        self, client: TestClient
    ) -> None:
        ids = [make_exception(client, po) for po in ("PO-4821", "PO-4822", "PO-4823")]
        body = client.post("/api/exceptions/resolve", json={"exception_ids": ids}).json()
        assert body["dispatched"] == 3
        assert body["provider_call_id"].startswith("call_")
        assert body["is_live"] is False

    def test_empty_id_list_resolves_everything_eligible(self, client: TestClient) -> None:
        make_exception(client, "PO-4821")
        make_exception(client, "PO-4822")
        body = client.post("/api/exceptions/resolve", json={"exception_ids": []}).json()
        assert body["dispatched"] == 2

    def test_nothing_eligible_is_reported_not_errored(self, client: TestClient) -> None:
        body = client.post("/api/exceptions/resolve", json={"exception_ids": []}).json()
        assert body["dispatched"] == 0
        assert "eligible" in body["message"].lower()

    def test_resolving_an_unknown_exception_is_404(self, client: TestClient) -> None:
        assert client.post("/api/exceptions/nope/resolve").status_code == 404

    def test_response_states_plainly_that_no_real_call_was_placed(
        self, client: TestClient
    ) -> None:
        # A demo must never be mistakable for a live call.
        exc = make_exception(client, "PO-4821")
        body = client.post(f"/api/exceptions/{exc}/resolve").json()
        assert body["is_live"] is False
        assert "simulated" in body["message"].lower()


class TestWebhook:
    def _body(self, call_id: str, event_id: str = "evt_api_1", **over) -> dict:
        return {
            "id": event_id,
            "type": "call.completed",
            "created_at": datetime.now(UTC).isoformat(),
            "data": {"id": call_id, "status": "completed", "recipients": []},
            **over,
        }

    def _dispatch(self, client: TestClient) -> str:
        exc = make_exception(client, "PO-4821")
        return client.post(f"/api/exceptions/{exc}/resolve").json()["provider_call_id"]

    def test_first_delivery_is_accepted(self, client: TestClient) -> None:
        call_id = self._dispatch(client)
        response = client.post(
            "/webhooks/calle",
            json=self._body(call_id),
            headers={"CALL-E-Event-Id": "evt_api_1"},
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True, "duplicate": False, "event_id": "evt_api_1"}

    def test_redelivery_is_reported_as_a_duplicate(self, client: TestClient) -> None:
        # CALL-E delivery is at-least-once; a duplicate must be a no-op
        # AND must return 2xx, or the provider keeps retrying.
        call_id = self._dispatch(client)
        body = self._body(call_id)
        first = client.post("/webhooks/calle", json=body, headers={"CALL-E-Event-Id": "evt_api_1"})
        second = client.post("/webhooks/calle", json=body, headers={"CALL-E-Event-Id": "evt_api_1"})
        assert first.json()["duplicate"] is False
        assert second.status_code == 200
        assert second.json()["duplicate"] is True

    @pytest.mark.parametrize(
        "event_type", ["call.completed", "call.failed", "call.result_validation_failed"]
    )
    def test_all_three_documented_event_types_are_accepted(
        self, client: TestClient, event_type: str
    ) -> None:
        call_id = self._dispatch(client)
        response = client.post(
            "/webhooks/calle",
            json=self._body(call_id, event_id=f"evt_{event_type}", type=event_type),
            headers={"CALL-E-Event-Id": f"evt_{event_type}"},
        )
        assert response.status_code == 200

    @pytest.mark.parametrize(
        "bad",
        [
            {"type": "call.completed", "data": {"id": "call_1"}},
            {"id": "evt_1", "data": {"id": "call_1"}},
            {"id": "evt_1", "type": "call.exploded", "data": {"id": "call_1"}},
            {"id": "evt_1", "type": "call.completed"},
            {},
        ],
    )
    def test_malformed_bodies_are_rejected_with_400(
        self, client: TestClient, bad: dict
    ) -> None:
        # A 400 is correct: redelivering the same broken body cannot help.
        assert client.post("/webhooks/calle", json=bad).status_code == 400

    def test_webhook_enqueues_reconciliation_rather_than_deciding(
        self, client: TestClient, db: Session
    ) -> None:
        from app.db.models import ExceptionRecord, OutboxMessage

        exc = make_exception(client, "PO-4821")
        call_id = client.post(f"/api/exceptions/{exc}/resolve").json()["provider_call_id"]
        client.post(
            "/webhooks/calle",
            json=self._body(call_id),
            headers={"CALL-E-Event-Id": "evt_api_1"},
        )

        # The receiver must stay dumb: an outbox row, and no decision yet.
        pending = db.query(OutboxMessage).filter(OutboxMessage.status == "PENDING").all()
        assert len(pending) == 1
        assert pending[0].event_type == "reconcile_call"

        db.expire_all()
        assert db.get(ExceptionRecord, exc).state == "CALLING"


class TestNoSecretsInAnyResponse:
    def test_no_endpoint_leaks_a_bearer_token_or_key(self, client: TestClient) -> None:
        exc = make_exception(client, "PO-4821")
        client.post(f"/api/exceptions/{exc}/resolve")

        for path in ("/health", "/api/exceptions", f"/api/exceptions/{exc}"):
            raw = client.get(path).text
            assert "iams_" not in raw, path
            assert "Bearer " not in raw, path
            assert "CALLE_API_KEY" not in raw, path


class TestRequestCorrelation:
    """Master prompt, Observability: every execution carries a request id."""

    def test_every_response_carries_a_request_id(self, client: TestClient) -> None:
        response = client.get("/api/exceptions")
        assert response.headers.get("X-Request-ID", "").startswith("req_")

    def test_a_callers_request_id_is_honoured(self, client: TestClient) -> None:
        # A reverse proxy's trace id must survive into our logs rather
        # than being replaced by one of ours.
        response = client.get("/api/exceptions", headers={"X-Request-ID": "trace-abc-123"})
        assert response.headers["X-Request-ID"] == "trace-abc-123"

    def test_an_oversized_inbound_id_is_truncated(self, client: TestClient) -> None:
        response = client.get("/api/exceptions", headers={"X-Request-ID": "x" * 5000})
        assert len(response.headers["X-Request-ID"]) <= 128

    def test_a_non_printable_inbound_id_is_replaced(self, client: TestClient) -> None:
        response = client.get("/api/exceptions", headers={"X-Request-ID": "bad\x00id"})
        assert response.headers["X-Request-ID"].startswith("req_")

    def test_each_request_gets_a_distinct_id(self, client: TestClient) -> None:
        ids = {client.get("/api/exceptions").headers["X-Request-ID"] for _ in range(5)}
        assert len(ids) == 5

    def test_the_id_reaches_log_records_without_being_passed_explicitly(self) -> None:
        # The whole point of the contextvar: policy code deep in the
        # stack is correlated without knowing HTTP exists.
        import logging

        from app.logging_setup import RequestIdFilter, reset_request_id, set_request_id

        token = set_request_id("req_deep")
        try:
            record = logging.LogRecord("x", logging.INFO, "f", 1, "msg", (), None)
            RequestIdFilter().filter(record)
            assert record.request_id == "req_deep"
        finally:
            reset_request_id(token)
