"""Provider-contract tests.

Each test here pins a fact from ``calle.openapi.yaml``. They are cheap
and they guard the failure mode that is hardest to debug: a schema or
field-name mistake does not raise, it just makes every structured result
come back ``null`` after you have spent real call credits finding out.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import httpx
import pytest

from app.adapters.calle.client import CalleClient
from app.adapters.calle.mapper import extract_webhook_call, map_call
from app.adapters.calle.mock import MockCalleProvider
from app.adapters.calle.schemas import (
    RECIPIENT_RESULT_SCHEMA,
    TASK_RESULT_SCHEMA,
    render_task,
)
from app.domain.commands import (
    CallRecipientCommand,
    CreateCallCommand,
    compute_batch_idempotency_key,
)
from app.domain.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimited,
    ProviderTransientError,
    ValidationError,
)

UNSUPPORTED_KEYWORDS = ("$ref", "oneOf", "anyOf", "allOf", "$defs", "not")

#: Fictional buyer company for every task rendered in this file.
#: render_task() now requires one -- see docs/provider-truth.md §5
#: (caller disclosure) -- so the agent never has to invent who it
#: represents.
TEST_BUYER_COMPANY = "Northwind Trading (test)"

#: Reserved recipient-result field names. Using one returns null results.
RESERVED_RECIPIENT_FIELDS = frozenset(
    {"summary", "status", "transcript", "call_id", "created_at", "completed_at"}
)


def _walk(node: Any):
    yield node
    if isinstance(node, dict):
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def make_recipients(pos: list[str]) -> tuple[CallRecipientCommand, ...]:
    return tuple(
        CallRecipientCommand(
            workflow_run_id=f"wr_{i}",
            exception_id=f"exc_{i}",
            attempt_no=1,
            po_number=po,
            supplier_name=f"Supplier {i}",
            recipient_name="Jordan",
            phone_e164=f"+1555000111{i}",
        )
        for i, po in enumerate(pos, start=1)
    )


def make_command(pos: list[str] | None = None, **overrides: Any) -> CreateCallCommand:
    recipients = make_recipients(pos or ["PO-4821"])
    defaults: dict[str, Any] = {
        "recipients": recipients,
        "task": render_task(
            [
                {
                    "supplier_name": r.supplier_name,
                    "recipient_name": r.recipient_name,
                    "po_number": r.po_number,
                    "phone": r.phone_e164,
                }
                for r in recipients
            ],
            buyer_company=TEST_BUYER_COMPANY,
        ),
        "result_schema": TASK_RESULT_SCHEMA,
        "recipient_result_schema": RECIPIENT_RESULT_SCHEMA,
        "idempotency_key": compute_batch_idempotency_key(recipients),
        "metadata": {"attempt_no": 1},
    }
    return CreateCallCommand(**{**defaults, **overrides})


# ───────────────────────── schema constraints ────────────────────────


class TestResultSchemaConstraints:
    """docs/provider-truth.md §4."""

    @pytest.mark.parametrize(
        "schema", [RECIPIENT_RESULT_SCHEMA, TASK_RESULT_SCHEMA], ids=["recipient", "task"]
    )
    def test_uses_no_unsupported_json_schema_keyword(self, schema: dict) -> None:
        # Inspect schema KEYS, not raw JSON text -- "not" occurs
        # legitimately inside prose descriptions.
        for node in _walk(schema):
            if isinstance(node, dict):
                used = set(node) & set(UNSUPPORTED_KEYWORDS)
                assert not used, f"{used} not supported by CALL-E result schemas"

    @pytest.mark.parametrize(
        "schema", [RECIPIENT_RESULT_SCHEMA, TASK_RESULT_SCHEMA], ids=["recipient", "task"]
    )
    def test_every_object_is_strict(self, schema: dict) -> None:
        for node in _walk(schema):
            if isinstance(node, dict) and node.get("type") == "object":
                assert node.get("additionalProperties") is False

    def test_recipient_schema_avoids_reserved_field_names(self) -> None:
        # This is why the field is po_status and not status.
        used = set(RECIPIENT_RESULT_SCHEMA["properties"])
        collisions = used & RESERVED_RECIPIENT_FIELDS
        assert not collisions, f"reserved recipient field names used: {collisions}"

    def test_recipient_schema_carries_the_seven_documented_fields(self) -> None:
        # v2: spoke_with and escalation_reason were added because the
        # original five fields could not represent who was reached or
        # differentiate why a human is needed. See docs/provider-truth.md
        # §5-6 and the SCHEMA_VERSION bump to supplier-exception.v2.
        assert set(RECIPIENT_RESULT_SCHEMA["required"]) == {
            "received",
            "po_status",
            "ship_date",
            "blocker",
            "needs_human",
            "spoke_with",
            "escalation_reason",
        }

    def test_schema_version_reflects_the_v2_evidence_fields(self) -> None:
        from app.adapters.calle.schemas import SCHEMA_VERSION

        assert SCHEMA_VERSION == "supplier-exception.v2"

    def test_uncertain_enums_all_offer_unknown(self) -> None:
        # "Prefer string enums over booleans ... include an unknown enum
        # value when the call may not provide enough evidence."
        for name in ("received", "po_status", "blocker", "needs_human"):
            assert "unknown" in RECIPIENT_RESULT_SCHEMA["properties"][name]["enum"]

    def test_every_property_has_a_description(self) -> None:
        # Descriptions are passed to the extraction model.
        for name, prop in RECIPIENT_RESULT_SCHEMA["properties"].items():
            assert prop.get("description"), f"{name} has no description"


# ─────────────────────────── task rendering ──────────────────────────


class TestTaskRendering:
    def test_single_recipient_task_names_the_po_and_supplier(self) -> None:
        task = render_task(
            [
                {
                    "supplier_name": "Acme Components",
                    "recipient_name": "Jordan",
                    "po_number": "PO-4821",
                    "phone": "+15550001111",
                }
            ],
            buyer_company=TEST_BUYER_COMPANY,
        )
        assert "PO-4821" in task
        assert "Acme Components" in task
        assert "Do not negotiate" in task

    def test_batch_task_isolates_recipients_from_each_other(self) -> None:
        task = render_task(
            [
                {
                    "supplier_name": "Acme",
                    "recipient_name": "Jordan",
                    "po_number": "PO-4821",
                    "phone": "+15550001111",
                },
                {
                    "supplier_name": "Globex",
                    "recipient_name": "Sam",
                    "po_number": "PO-4822",
                    "phone": "+15550002222",
                },
            ],
            buyer_company=TEST_BUYER_COMPANY,
        )
        # The instruction not to cross-disclose is the whole reason a
        # batch call is safe to make.
        assert "Never mention" in task
        assert "+15550001111" in task and "+15550002222" in task

    def test_empty_context_is_refused(self) -> None:
        with pytest.raises(ValueError, match="at least one recipient"):
            render_task([], buyer_company=TEST_BUYER_COMPANY)


class TestCallerDisclosure:
    """The agent must truthfully identify itself and who it represents.

    docs/prompts/resolve-e-system.md requires the agent to "identify
    yourself and the company you represent" as the first allowed
    objective. The live transcript in
    ``tests/fixtures/real_call_connected.json`` showed this was NOT
    happening: the agent opened with "Hi, am I speaking with the
    supplier contact" and never said it was an AI or named a company.
    These tests pin the fix.
    """

    def _task(self, **over: object) -> str:
        ctx = [
            {
                "supplier_name": "Acme Components",
                "recipient_name": "Jordan",
                "po_number": "PO-4821",
                "phone": "+15550001111",
            }
        ]
        return render_task(ctx, buyer_company=over.get("buyer_company", TEST_BUYER_COMPANY))  # type: ignore[arg-type]

    def test_the_task_names_the_buyer_company(self) -> None:
        task = self._task(buyer_company="Contoso Manufacturing")
        assert "Contoso Manufacturing" in task

    def test_the_task_requires_disclosing_ai_status(self) -> None:
        task = self._task()
        # Not merely "AI" appearing anywhere -- the opening instruction
        # must actually tell the agent to say it is an AI assistant.
        assert "AI assistant" in task
        assert "say plainly that you are an AI assistant" in task

    def test_the_task_orders_identification_before_order_details(self) -> None:
        task = self._task()
        open_idx = task.index("HOW TO OPEN THE CALL")
        ask_idx = task.index("ASK, IN THIS ORDER")
        confirm_contact_idx = task.index("Confirm you are speaking with the named contact")
        assert open_idx < ask_idx < confirm_contact_idx

    def test_disclosure_allowlist_includes_the_ai_and_company_statement(self) -> None:
        task = self._task(buyer_company="Contoso Manufacturing")
        allow_block = task[task.index("YOU MAY DISCLOSE ONLY") :]
        assert "AI assistant calling for Contoso Manufacturing" in allow_block

    def test_a_blank_buyer_company_is_refused_not_defaulted(self) -> None:
        # A placeholder here would be spoken aloud to a real person.
        with pytest.raises(ValueError, match="buyer_company"):
            render_task(
                [
                    {
                        "supplier_name": "Acme",
                        "recipient_name": "Jordan",
                        "po_number": "PO-1",
                        "phone": "+15550001111",
                    }
                ],
                buyer_company="   ",
            )

    def test_no_placeholder_company_name_leaks_into_a_real_task(self) -> None:
        task = self._task(buyer_company="Contoso Manufacturing")
        for placeholder in ("Resolve-E", "an authorized buyer", "TODO", "{buyer_company}"):
            assert placeholder not in task, placeholder


# ───────────────────────────── idempotency ───────────────────────────


class TestIdempotencyKey:
    def test_is_stable_for_the_same_logical_batch(self) -> None:
        r = make_recipients(["PO-4821", "PO-4822"])
        assert compute_batch_idempotency_key(r) == compute_batch_idempotency_key(r)

    def test_is_order_independent(self) -> None:
        r = make_recipients(["PO-4821", "PO-4822"])
        assert compute_batch_idempotency_key(r) == compute_batch_idempotency_key(
            tuple(reversed(r))
        )

    def test_differs_when_batch_membership_differs(self) -> None:
        a = compute_batch_idempotency_key(make_recipients(["PO-4821"]))
        b = compute_batch_idempotency_key(make_recipients(["PO-4821", "PO-4822"]))
        assert a != b

    def test_differs_across_attempts(self) -> None:
        first = make_recipients(["PO-4821"])
        second = tuple(dataclasses.replace(r, attempt_no=2) for r in first)
        assert compute_batch_idempotency_key(first) != compute_batch_idempotency_key(second)

    def test_fits_the_provider_length_limit(self) -> None:
        key = compute_batch_idempotency_key(make_recipients([f"PO-{i}" for i in range(50)]))
        assert 1 <= len(key) <= 255

    def test_empty_batch_is_refused(self) -> None:
        with pytest.raises(ValueError):
            compute_batch_idempotency_key(())


# ───────────────────────────── the mapper ────────────────────────────


class TestMapper:
    def test_maps_a_terminal_call_task(self) -> None:
        call = map_call(
            {
                "id": "call_1",
                "object": "call_task",
                "status": "completed",
                "recipients": [
                    {
                        "id": "rcp_1",
                        "phones": ["+15550001111"],
                        "status": "completed",
                        "structured_result": {"received": "yes"},
                        "summary": "ok",
                        "attempts": [{"id": "a1"}],
                    }
                ],
                "structured_result": {"suppliers_reached": 1},
                "summary": "done",
                "task_completed": True,
                "completion_confidence": {"score": 0.9, "label": "high"},
                "evidence": ["said yes"],
                "metadata": {"exception_id": "exc_1"},
                "failure_code": None,
                "failure_message": None,
                "created_at": "2026-09-09T12:00:00Z",
                "completed_at": "2026-09-09T12:03:00Z",
            }
        )
        assert call.provider_call_id == "call_1"
        assert call.is_terminal
        assert call.completion_confidence is not None
        assert call.completion_confidence.score == 0.9
        assert call.recipients[0].reached
        assert call.created_at is not None and call.completed_at is not None

    def test_nulls_while_running_do_not_raise(self) -> None:
        call = map_call(
            {
                "id": "call_2",
                "status": "in_progress",
                "recipients": [],
                "structured_result": None,
                "summary": None,
                "task_completed": None,
                "completion_confidence": None,
                "evidence": [],
                "metadata": {},
            }
        )
        assert not call.is_terminal
        assert call.task_completed is None
        assert call.completion_confidence is None

    def test_unknown_recipient_status_is_passed_through_not_coerced(self) -> None:
        call = map_call(
            {
                "id": "call_3",
                "status": "completed",
                "recipients": [{"id": "r", "phones": [], "status": "quarantined"}],
            }
        )
        # Never silently promoted to "completed".
        assert call.recipients[0].status == "quarantined"
        assert not call.recipients[0].reached

    def test_non_dict_structured_result_becomes_none(self) -> None:
        call = map_call(
            {"id": "c", "status": "completed", "recipients": [], "structured_result": "oops"}
        )
        assert call.raw_structured_result is None


class TestWebhookExtraction:
    def _body(self, **over: Any) -> dict[str, Any]:
        return {
            "id": "evt_1",
            "type": "call.completed",
            "created_at": "2026-09-09T12:03:00Z",
            "data": {"id": "call_1", "status": "completed", "recipients": []},
            **over,
        }

    def test_extracts_event_id_type_and_call(self) -> None:
        eid, etype, call = extract_webhook_call(self._body())
        assert (eid, etype, call.provider_call_id) == ("evt_1", "call.completed", "call_1")

    @pytest.mark.parametrize(
        "event_type", ["call.completed", "call.failed", "call.result_validation_failed"]
    )
    def test_all_three_documented_event_types_are_accepted(self, event_type: str) -> None:
        # call.result_validation_failed is the one the blueprint missed.
        _, etype, _ = extract_webhook_call(self._body(type=event_type))
        assert etype == event_type

    @pytest.mark.parametrize(
        "bad",
        [
            {"type": "call.completed", "data": {"id": "call_1"}},  # no event id
            {"id": "evt_1", "data": {"id": "call_1"}},  # no type
            {"id": "evt_1", "type": "call.exploded", "data": {"id": "c"}},  # unknown type
            {"id": "evt_1", "type": "call.completed"},  # no data
            {"id": "evt_1", "type": "call.completed", "data": {"status": "completed"}},
        ],
    )
    def test_malformed_bodies_are_rejected(self, bad: dict) -> None:
        with pytest.raises(ValueError):
            extract_webhook_call(bad)


# ──────────────────────── client error mapping ───────────────────────


def client_returning(*responses: httpx.Response) -> CalleClient:
    it = iter(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        return next(it)

    transport = httpx.MockTransport(handler)
    return CalleClient(
        "iams_test_key",
        client=httpx.Client(transport=transport, base_url="https://api.heycall-e.com"),
    )


class TestClientErrorMapping:
    def test_401_is_an_auth_error_and_is_not_retryable(self) -> None:
        c = client_returning(httpx.Response(401, json={"error": {"code": "unauthorized"}}))
        with pytest.raises(ProviderAuthError) as exc:
            c.create_call(make_command())
        assert not exc.value.retryable

    def test_429_is_rate_limited_and_retryable(self) -> None:
        c = client_returning(*[httpx.Response(429, json={"error": {"code": "rate_limited"}})] * 3)
        with pytest.raises(ProviderRateLimited) as exc:
            c.create_call(make_command())
        assert exc.value.retryable

    def test_400_is_permanent(self) -> None:
        c = client_returning(httpx.Response(400, json={"error": {"code": "invalid_request"}}))
        with pytest.raises(ProviderError) as exc:
            c.create_call(make_command())
        assert not exc.value.retryable

    def test_500_is_transient_and_retried_before_giving_up(self) -> None:
        c = client_returning(*[httpx.Response(500, text="boom")] * 3)
        with pytest.raises(ProviderTransientError):
            c.create_call(make_command())

    def test_transient_then_success_returns_the_call(self) -> None:
        ok = {"id": "call_9", "status": "queued", "recipients": [{"id": "r1", "phones": []}]}
        c = client_returning(httpx.Response(503, text="unavailable"), httpx.Response(201, json=ok))
        handle = c.create_call(make_command())
        assert handle.provider_call_id == "call_9"

    def test_retry_reuses_the_same_idempotency_key(self) -> None:
        """The whole duplicate-call guarantee rests on this."""
        seen: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("Idempotency-Key"))
            if len(seen) < 3:
                return httpx.Response(503, text="unavailable")
            return httpx.Response(
                201, json={"id": "call_9", "status": "queued", "recipients": []}
            )

        c = CalleClient(
            "iams_test_key",
            client=httpx.Client(
                transport=httpx.MockTransport(handler), base_url="https://api.heycall-e.com"
            ),
        )
        cmd = make_command()
        c.create_call(cmd)
        assert seen == [cmd.idempotency_key] * 3
        assert len(set(seen)) == 1

    def test_200_on_create_is_reported_as_deduplicated(self) -> None:
        # Provider replayed an existing call for a reused key.
        c = client_returning(
            httpx.Response(200, json={"id": "call_9", "status": "queued", "recipients": []})
        )
        assert c.create_call(make_command()).deduplicated is True


class TestClientRequestShape:
    def test_builds_a_valid_create_call_request(self) -> None:
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            captured["_idem"] = request.headers.get("Idempotency-Key")
            captured["_auth"] = request.headers.get("Authorization")
            return httpx.Response(201, json={"id": "call_1", "status": "queued", "recipients": []})

        c = CalleClient(
            "iams_test_key",
            client=httpx.Client(
                transport=httpx.MockTransport(handler), base_url="https://api.heycall-e.com"
            ),
        )
        c.create_call(make_command(["PO-4821", "PO-4822"]))

        assert captured["_auth"] == "Bearer iams_test_key"
        assert captured["_idem"].startswith("resolve-e:batch:")
        # recipients[] is a list of objects each holding a phones[] list.
        assert captured["recipients"] == [
            {"phones": ["+15550001111"], "region": "US", "locale": "en-US"},
            {"phones": ["+15550001112"], "region": "US", "locale": "en-US"},
        ]
        assert captured["recipient_result_schema"] == RECIPIENT_RESULT_SCHEMA
        assert captured["result_schema"] == TASK_RESULT_SCHEMA
        # No undeclared keys: the provider schema is additionalProperties:false.
        assert set(captured) <= {
            "task", "recipients", "result_schema", "recipient_result_schema",
            "metadata", "webhook_url", "_idem", "_auth",
        }

    def test_invalid_phone_is_refused_before_any_network_call(self) -> None:
        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(201, json={})

        c = CalleClient(
            "iams_test_key",
            client=httpx.Client(
                transport=httpx.MockTransport(handler), base_url="https://api.heycall-e.com"
            ),
        )
        bad = dataclasses.replace(
            make_command(),
            recipients=(
                CallRecipientCommand(
                    workflow_run_id="wr", exception_id="exc", attempt_no=1,
                    po_number="PO-1", supplier_name="S", recipient_name=None,
                    phone_e164="555-0100",
                ),
            ),
        )
        with pytest.raises(ValidationError):
            c.create_call(bad)
        assert not called, "no request should reach the provider"

    def test_get_call_rejects_a_non_call_id(self) -> None:
        c = client_returning(httpx.Response(200, json={}))
        with pytest.raises(ValidationError):
            c.get_call("wr_123")

    def test_empty_api_key_is_refused(self) -> None:
        with pytest.raises(ValidationError):
            CalleClient("")


# ──────────────────────────── the mock double ────────────────────────


class TestMockHonoursProviderSemantics:
    def test_same_key_returns_the_original_call(self) -> None:
        p = MockCalleProvider()
        cmd = make_command(["PO-4821"])
        a, b = p.create_call(cmd), p.create_call(cmd)
        assert a.provider_call_id == b.provider_call_id
        assert b.deduplicated
        assert p.call_count == 1

    def test_create_timeout_retried_with_same_key_places_one_call(self) -> None:
        p = MockCalleProvider()
        p.fail_next_create = True
        cmd = make_command(["PO-4821"])
        with pytest.raises(ProviderTransientError):
            p.create_call(cmd)
        p.create_call(cmd)
        assert p.call_count == 1

    def test_batch_produces_independent_per_recipient_results(self) -> None:
        p = MockCalleProvider()
        handle = p.create_call(make_command(["PO-4821", "PO-4822", "PO-4823"]))
        call = p.get_call(handle.provider_call_id)
        statuses = [
            (r.raw_structured_result or {}).get("po_status") for r in call.recipients
        ]
        assert statuses == ["on_time", "delayed", "unknown"]

    def test_deferred_call_is_not_terminal_until_finalized(self) -> None:
        p = MockCalleProvider()
        p.defer_terminal = True
        handle = p.create_call(make_command(["PO-4821"]))
        assert not p.get_call(handle.provider_call_id).is_terminal
        p.finalize(handle.provider_call_id)
        assert p.get_call(handle.provider_call_id).is_terminal

    def test_webhook_body_round_trips_through_the_real_extractor(self) -> None:
        p = MockCalleProvider()
        handle = p.create_call(make_command(["PO-4821"]))
        body = p.webhook_body(handle.provider_call_id)
        eid, etype, call = extract_webhook_call(body)
        assert eid.startswith("evt_")
        assert etype == "call.completed"
        assert call.provider_call_id == handle.provider_call_id


class TestTranscriptMapping:
    """`transcript_turns` hangs off recipients[].attempts[], not the call task."""

    def test_maps_turns_from_a_nested_attempt(self) -> None:
        call = map_call(
            {
                "id": "call_1",
                "status": "completed",
                "recipients": [
                    {
                        "id": "rcp_1",
                        "phones": ["+15550001111"],
                        "status": "completed",
                        "attempts": [
                            {
                                "id": "att_1",
                                "phone": "+15550001111",
                                "status": "completed",
                                "transcript_turns": [
                                    {"offset_seconds": 0, "speaker": "bot", "text": "Hello."},
                                    {"offset_seconds": 4, "speaker": "user", "text": "Speaking."},
                                ],
                            }
                        ],
                    }
                ],
            }
        )
        turns = call.recipients[0].transcript
        assert [t.speaker for t in turns] == ["bot", "user"]
        assert turns[1].text == "Speaking."
        assert turns[0].offset_seconds == 0

    def test_a_null_offset_is_preserved_not_zeroed(self) -> None:
        # The spec says offset_seconds is null when the source line had
        # no parseable timestamp. Zero would be a different claim.
        call = map_call(
            {
                "id": "c",
                "status": "completed",
                "recipients": [
                    {
                        "id": "r",
                        "phones": [],
                        "status": "completed",
                        "attempts": [
                            {
                                "id": "a",
                                "status": "completed",
                                "transcript_turns": [
                                    {"offset_seconds": None, "speaker": "bot", "text": "Hi."}
                                ],
                            }
                        ],
                    }
                ],
            }
        )
        assert call.recipients[0].transcript[0].offset_seconds is None

    def test_turns_without_text_are_dropped(self) -> None:
        call = map_call(
            {
                "id": "c",
                "status": "completed",
                "recipients": [
                    {
                        "id": "r",
                        "phones": [],
                        "status": "completed",
                        "attempts": [
                            {
                                "id": "a",
                                "status": "completed",
                                "transcript_turns": [
                                    {"speaker": "bot", "text": ""},
                                    {"speaker": "bot"},
                                    {"speaker": "user", "text": "Real line."},
                                ],
                            }
                        ],
                    }
                ],
            }
        )
        turns = call.recipients[0].transcript
        assert len(turns) == 1
        assert turns[0].text == "Real line."

    def test_transcript_comes_from_the_last_attempt_that_has_one(self) -> None:
        # Earlier attempts are dials that did not connect.
        call = map_call(
            {
                "id": "c",
                "status": "completed",
                "recipients": [
                    {
                        "id": "r",
                        "phones": [],
                        "status": "completed",
                        "attempts": [
                            {"id": "a1", "status": "failed", "transcript_turns": []},
                            {
                                "id": "a2",
                                "status": "completed",
                                "transcript_turns": [
                                    {"speaker": "user", "text": "Second attempt."}
                                ],
                            },
                        ],
                    }
                ],
            }
        )
        assert call.recipients[0].transcript[0].text == "Second attempt."
        assert call.recipients[0].attempt_count == 2

    def test_missing_attempts_yield_an_empty_transcript_not_an_error(self) -> None:
        call = map_call(
            {"id": "c", "status": "completed", "recipients": [{"id": "r", "phones": []}]}
        )
        assert call.recipients[0].transcript == ()
        assert call.recipients[0].attempt_count == 0

    def test_the_simulator_produces_a_readable_conversation(self) -> None:
        p = MockCalleProvider()
        handle = p.create_call(make_command(["PO-4822"]))
        turns = p.get_call(handle.provider_call_id).recipients[0].transcript
        assert len(turns) >= 2
        assert {t.speaker for t in turns} == {"bot", "user"}
        # The scripted supplier line must actually appear.
        assert any("inventory is delayed" in t.text for t in turns)

    def test_transcript_never_reaches_the_policy_engine(self) -> None:
        """Policy reads the structured result, never the words."""
        import inspect

        from app.policies import evidence_policy, transition_policy

        for module in (transition_policy, evidence_policy):
            source = inspect.getsource(module)
            assert "transcript" not in source, (
                f"{module.__name__} must not read the transcript: a decision that "
                "depends on wording is not deterministic"
            )


class TestOfficialSdkProvider:
    """The official `calle-ai` SDK is the default transport for live calls.

    These tests pin the two properties that matter: the SDK provider is
    interchangeable with the hand-rolled one at the adapter boundary, and
    no SDK-specific exception can escape into business code, where retry
    decisions are made from ErrorClass alone.
    """

    def test_it_satisfies_the_same_provider_protocol(self) -> None:
        from app.adapters.calle.client import CallProvider
        from app.adapters.calle.sdk_client import SdkCalleProvider

        provider = SdkCalleProvider("iams_test_key")
        assert isinstance(provider, CallProvider)
        for method in ("create_call", "get_call", "list_events"):
            assert callable(getattr(provider, method))

    def test_an_empty_key_is_refused(self) -> None:
        from app.adapters.calle.sdk_client import SdkCalleProvider

        with pytest.raises(ValidationError):
            SdkCalleProvider("")

    def test_a_non_call_id_is_refused_before_any_request(self) -> None:
        from app.adapters.calle.sdk_client import SdkCalleProvider

        provider = SdkCalleProvider("iams_test_key")
        with pytest.raises(ValidationError):
            provider.get_call("wr_123")

    def test_an_invalid_phone_is_refused_before_any_request(self) -> None:
        import dataclasses

        from app.adapters.calle.sdk_client import SdkCalleProvider

        provider = SdkCalleProvider("iams_test_key")
        bad = dataclasses.replace(
            make_command(),
            recipients=(
                CallRecipientCommand(
                    workflow_run_id="wr", exception_id="exc", attempt_no=1,
                    po_number="PO-1", supplier_name="S", recipient_name=None,
                    phone_e164="555-0100",
                ),
            ),
        )
        with pytest.raises(ValidationError):
            provider.create_call(bad)

    @pytest.mark.parametrize(
        ("sdk_error", "expected", "retryable"),
        [
            ("CalleAuthenticationError", ProviderAuthError, False),
            ("CalleRateLimitError", ProviderRateLimited, True),
            ("CalleTimeoutError", ProviderTransientError, True),
            ("CalleConnectionError", ProviderTransientError, True),
        ],
    )
    def test_sdk_errors_are_translated_into_the_internal_taxonomy(
        self, sdk_error: str, expected: type, retryable: bool
    ) -> None:
        import calle.errors as sdk_errors

        from app.adapters.calle.sdk_client import _translated_errors

        cls = getattr(sdk_errors, sdk_error)
        exc = (
            cls("boom")
            if sdk_error in ("CalleTimeoutError", "CalleConnectionError")
            else cls(code="x", message="boom", status_code=401 if "Auth" in sdk_error else 429)
        )
        with pytest.raises(expected) as raised, _translated_errors("op"):
            raise exc
        assert raised.value.retryable is retryable

    def test_a_5xx_is_transient_and_a_4xx_is_not(self) -> None:
        from calle.errors import CalleAPIError

        from app.adapters.calle.sdk_client import _translated_errors

        with pytest.raises(ProviderTransientError), _translated_errors("op"):
            raise CalleAPIError(code="x", message="server", status_code=503)

        with pytest.raises(ProviderError) as raised, _translated_errors("op"):
            raise CalleAPIError(code="x", message="bad body", status_code=400)
        assert not raised.value.retryable

    def test_no_sdk_exception_type_escapes_the_adapter(self) -> None:
        """Business code branches on ErrorClass, never on a provider type."""
        import calle.errors as sdk_errors

        from app.adapters.calle.sdk_client import _translated_errors

        for name in (
            "CalleAuthenticationError",
            "CalleRateLimitError",
            "CalleTimeoutError",
            "CalleConnectionError",
            "CalleAPIError",
        ):
            cls = getattr(sdk_errors, name)
            exc = (
                cls("boom")
                if name in ("CalleTimeoutError", "CalleConnectionError")
                else cls(code="x", message="boom", status_code=500)
            )
            try:
                with _translated_errors("op"):
                    raise exc
            except Exception as got:
                assert not type(got).__module__.startswith("calle"), name

    def test_our_request_shape_matches_the_sdk_exactly(self) -> None:
        """The hand-rolled client and the SDK must build the same request.

        This is the closest thing to a check on whether our reading of
        the API is right rather than merely self-consistent.
        """
        import inspect

        from calle.calls import CalleCalls

        source = inspect.getsource(CalleCalls.create)
        # The SDK posts these exact keys; so do we (see
        # CalleClient._build_create_payload).
        for key in (
            '"task"',
            '"recipients"',
            '"result_schema"',
            '"recipient_result_schema"',
            '"metadata"',
            '"webhook_url"',
        ):
            assert key in source, f"SDK no longer sends {key}"
        assert '"Idempotency-Key"' in source, "idempotency moved off the header"
        assert '"/v1/calls"' in source


def test_all_three_transports_satisfy_the_provider_protocol() -> None:
    """SDK, raw HTTP, and simulator are interchangeable at the boundary."""
    from app.adapters.calle.client import CalleClient, CallProvider
    from app.adapters.calle.mock import MockCalleProvider
    from app.adapters.calle.sdk_client import SdkCalleProvider

    for provider in (
        SdkCalleProvider("iams_test_key"),
        CalleClient("iams_test_key"),
        MockCalleProvider(),
    ):
        assert isinstance(provider, CallProvider), type(provider).__name__


class TestRegionInference:
    """`region` feeds routing and compliance, so a wrong hint is worse than none."""

    @pytest.mark.parametrize(
        ("phone", "region", "locale"),
        [
            ("+919876543210", "IN", "en-IN"),
            ("+15550001111", "US", "en-US"),
            ("+447700900123", "GB", "en-GB"),
            ("+6591234567", "SG", "en-SG"),
            ("+971501234567", "AE", "en-AE"),
        ],
    )
    def test_region_is_inferred_from_the_dial_code(
        self, phone: str, region: str, locale: str
    ) -> None:
        rc = CallRecipientCommand(
            workflow_run_id="w", exception_id="e", attempt_no=1,
            po_number="PO-1", supplier_name="S", recipient_name=None, phone_e164=phone,
        )
        assert (rc.region, rc.locale) == (region, locale)

    def test_an_unknown_dial_code_sends_no_hint_rather_than_a_wrong_one(self) -> None:
        rc = CallRecipientCommand(
            workflow_run_id="w", exception_id="e", attempt_no=1,
            po_number="PO-1", supplier_name="S", recipient_name=None,
            phone_e164="+2126612345",
        )
        assert rc.region is None and rc.locale is None

    def test_an_explicit_region_is_never_overridden(self) -> None:
        rc = CallRecipientCommand(
            workflow_run_id="w", exception_id="e", attempt_no=1,
            po_number="PO-1", supplier_name="S", recipient_name=None,
            phone_e164="+919876543210", region="AE", locale="ar-AE",
        )
        assert (rc.region, rc.locale) == ("AE", "ar-AE")

    def test_plus_one_does_not_shadow_plus_ninety_one(self) -> None:
        # Longest-prefix matching: a naive single-digit lookup would call
        # every Indian number American.
        from app.domain.models import region_and_locale_for

        assert region_and_locale_for("+919876543210")[0] == "IN"
        assert region_and_locale_for("+15550001111")[0] == "US"

    def test_an_indian_number_is_not_dispatched_as_us(self) -> None:
        """The bug this class exists for."""
        import json

        import httpx

        captured: dict = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured.update(json.loads(request.content))
            return httpx.Response(201, json={"id": "call_1", "status": "queued", "recipients": []})

        client = CalleClient(
            "iams_test_key",
            client=httpx.Client(
                transport=httpx.MockTransport(handler), base_url="https://api.heycall-e.com"
            ),
        )
        recipients = (
            CallRecipientCommand(
                workflow_run_id="w", exception_id="e", attempt_no=1,
                po_number="PO-1", supplier_name="S", recipient_name=None,
                phone_e164="+919876543210",
            ),
        )
        client.create_call(
            CreateCallCommand(
                recipients=recipients,
                task=render_task(
                    [{"supplier_name": "S", "recipient_name": "",
                      "po_number": "PO-1", "phone": "+919876543210"}],
                    buyer_company=TEST_BUYER_COMPANY,
                ),
                result_schema=TASK_RESULT_SCHEMA,
                recipient_result_schema=RECIPIENT_RESULT_SCHEMA,
                idempotency_key=compute_batch_idempotency_key(recipients),
            )
        )
        assert captured["recipients"][0]["region"] == "IN"
