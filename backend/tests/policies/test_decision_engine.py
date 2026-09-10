"""Decision-engine contract.

Every mandatory case from ``docs/testing.md`` and the master prompt's
test-first list lives here, plus the five conversation scenarios.

These tests are the specification. If a change to the engine makes one
of them fail, the change is wrong unless the documentation changed
first.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.domain.enums import Decision, ExceptionState, Outcome, ReasonCode
from app.policies.evidence_policy import classify_outcome, validate_recipient_result
from app.policies.transition_policy import DECISION_TO_STATE, decide
from tests.factories import (
    NOW,
    make_call,
    make_exception,
    make_recipient,
    make_result,
    make_settings,
)

SETTINGS = make_settings()


def run(
    *,
    exception=None,
    call=None,
    recipient=None,
    settings=None,
    reconciliation_available: bool = False,
):
    """Decide, with sensible defaults.

    ``reconciliation_available`` defaults to False so that tests state
    the *final* decision rather than the intermediate RECONCILE hop.
    """
    recipient = recipient if recipient is not None else make_recipient()
    call = call if call is not None else make_call(recipients=(recipient,))
    return decide(
        exception if exception is not None else make_exception(),
        call,
        recipient,
        settings or SETTINGS,
        now=NOW,
        reconciliation_available=reconciliation_available,
    )


# ─────────────────────────── Scenario A–E ────────────────────────────


class TestConversationScenarios:
    """The five transcripts in ``docs/testing.md``."""

    def test_scenario_a_on_time_resolves(self) -> None:
        # "Yes, we received PO-4821. We will ship Friday."
        r = make_recipient(result=make_result(received="yes", po_status="on_time"))
        d = run(recipient=r)
        assert d.decision is Decision.RESOLVE_ON_TIME
        assert d.reason_code is ReasonCode.EVIDENCE_SUFFICIENT_ON_TIME

    def test_scenario_b_delayed_resolves_as_delayed(self) -> None:
        # "We received it, but inventory is delayed. We can ship next Tuesday."
        r = make_recipient(
            result=make_result(
                received="yes",
                po_status="delayed",
                ship_date="next Tuesday",
                blocker="inventory",
            )
        )
        # ack_due 26h ago (2026-09-08); "next Tuesday" from 2026-09-09 is
        # 2026-09-22 -- beyond 48h, so widen the threshold for this case.
        d = run(recipient=r, settings=make_settings(delay_threshold_hours=24 * 20))
        assert d.decision is Decision.RESOLVE_DELAYED
        assert d.reason_code is ReasonCode.EVIDENCE_SUFFICIENT_DELAYED
        assert d.input_snapshot["parsed_ship_date"] == "2026-09-22"

    def test_scenario_c_ambiguous_escalates(self) -> None:
        # "I believe someone in logistics has it."
        r = make_recipient(
            result=make_result(received="unknown", po_status="unknown", ship_date="unknown")
        )
        d = run(recipient=r)
        assert d.decision is Decision.HUMAN_REVIEW
        assert d.reason_code is ReasonCode.EVIDENCE_AMBIGUOUS

    def test_scenario_d_commercial_negotiation_escalates(self) -> None:
        # "We can ship tomorrow if you increase the price."
        # The agent is instructed to set needs_human on a consequential ask.
        r = make_recipient(
            result=make_result(received="yes", po_status="delayed", needs_human="yes")
        )
        d = run(recipient=r)
        assert d.decision is Decision.HUMAN_REVIEW
        assert d.reason_code is ReasonCode.SUPPLIER_REQUESTED_HUMAN

    def test_scenario_e_wrong_person_does_not_resolve(self) -> None:
        # "I am not responsible for purchase orders."
        r = make_recipient(result=make_result(received="unknown", po_status="unknown"))
        d = run(recipient=r, exception=make_exception(attempt_count=1))
        assert d.decision is not Decision.RESOLVE_ON_TIME
        assert d.decision is Decision.HUMAN_REVIEW


# ─────────────────────────── Ordering rules ──────────────────────────


class TestBranchPrecedence:
    """The pseudocode's branch order encodes precedence, not style."""

    def test_needs_human_outranks_a_clean_on_time_answer(self) -> None:
        r = make_recipient(
            result=make_result(received="yes", po_status="on_time", needs_human="yes")
        )
        assert run(recipient=r).decision is Decision.HUMAN_REVIEW

    def test_terminal_exception_outranks_everything(self) -> None:
        d = run(exception=make_exception(state=ExceptionState.RESOLVED_ON_TIME))
        assert d.decision is Decision.NOOP
        assert d.reason_code is ReasonCode.TERMINAL_STATE_PROTECTED

    @pytest.mark.parametrize(
        "terminal",
        [
            ExceptionState.RESOLVED_ON_TIME,
            ExceptionState.RESOLVED_DELAYED,
            ExceptionState.HUMAN_REVIEW,
            ExceptionState.CLOSED_UNRESOLVED,
        ],
    )
    def test_late_result_never_moves_a_terminal_exception(
        self, terminal: ExceptionState
    ) -> None:
        d = run(exception=make_exception(state=terminal))
        assert d.decision is Decision.NOOP
        assert DECISION_TO_STATE[d.decision] is None

    def test_call_failure_outranks_any_structured_result(self) -> None:
        # A failed call task may still carry a stale result object.
        r = make_recipient(result=make_result(received="yes", po_status="on_time"))
        call = make_call(status="failed", recipients=(r,), failure_code="carrier_reject")
        d = run(recipient=r, call=call, exception=make_exception(attempt_count=1))
        assert d.decision is Decision.RETRY
        assert d.reason_code is ReasonCode.PROVIDER_CALL_FAILED


# ──────────────────────── Never resolve rules ────────────────────────


class TestNeverResolve:
    def test_null_structured_result_is_not_success(self) -> None:
        r = make_recipient(result=None)
        d = run(recipient=r, reconciliation_available=True)
        assert d.decision is Decision.RECONCILE
        assert d.reason_code is ReasonCode.STRUCTURED_RESULT_MISSING

    def test_null_result_after_reconciliation_retries_then_escalates(self) -> None:
        r = make_recipient(result=None)
        retryable = run(recipient=r, exception=make_exception(attempt_count=1))
        assert retryable.decision is Decision.RETRY

        exhausted = run(recipient=r, exception=make_exception(attempt_count=3))
        assert exhausted.decision is Decision.HUMAN_REVIEW

    def test_unreached_recipient_cannot_resolve_even_with_a_result(self) -> None:
        # The provider may attach a result to a recipient it never spoke
        # to. No conversation, no evidence.
        for status in ("failed", "skipped", "pending", "in_progress"):
            r = make_recipient(status=status, result=make_result())
            d = run(recipient=r, exception=make_exception(attempt_count=3))
            assert d.decision is not Decision.RESOLVE_ON_TIME, status
            assert d.reason_code is ReasonCode.RECIPIENT_NOT_REACHED, status

    def test_missing_required_field_is_rejected(self) -> None:
        bad = make_result()
        del bad["blocker"]
        d = run(recipient=make_recipient(result=bad), exception=make_exception(attempt_count=3))
        assert d.decision is Decision.HUMAN_REVIEW
        assert d.reason_code is ReasonCode.STRUCTURED_RESULT_INVALID

    def test_undeclared_extra_field_is_rejected(self) -> None:
        # additionalProperties:false is declared on the wire; we do not
        # trust that it was enforced.
        bad = {**make_result(), "internal_note": "ship it anyway"}
        d = run(recipient=make_recipient(result=bad), exception=make_exception(attempt_count=3))
        assert d.reason_code is ReasonCode.STRUCTURED_RESULT_INVALID

    def test_out_of_enum_value_is_rejected(self) -> None:
        bad = make_result(po_status="probably_fine")
        d = run(recipient=make_recipient(result=bad), exception=make_exception(attempt_count=3))
        assert d.reason_code is ReasonCode.STRUCTURED_RESULT_INVALID

    def test_non_string_field_is_rejected(self) -> None:
        bad = {**make_result(), "received": True}
        d = run(recipient=make_recipient(result=bad), exception=make_exception(attempt_count=3))
        assert d.reason_code is ReasonCode.STRUCTURED_RESULT_INVALID

    def test_delay_with_unusable_ship_date_escalates(self) -> None:
        r = make_recipient(
            result=make_result(received="yes", po_status="delayed", ship_date="sometime soon")
        )
        d = run(recipient=r)
        assert d.decision is Decision.HUMAN_REVIEW
        assert d.reason_code is ReasonCode.SHIP_DATE_UNUSABLE

    def test_delay_beyond_threshold_escalates(self) -> None:
        r = make_recipient(
            result=make_result(received="yes", po_status="delayed", ship_date="2026-10-30")
        )
        d = run(recipient=r)
        assert d.decision is Decision.HUMAN_REVIEW
        assert d.reason_code is ReasonCode.EVIDENCE_AMBIGUOUS

    def test_delay_without_receipt_is_inconsistent_and_escalates(self) -> None:
        r = make_recipient(
            result=make_result(received="unknown", po_status="delayed", ship_date="2026-09-10")
        )
        d = run(recipient=r)
        assert d.decision is Decision.HUMAN_REVIEW

    def test_blocked_escalates(self) -> None:
        r = make_recipient(
            result=make_result(received="yes", po_status="blocked", blocker="production")
        )
        d = run(recipient=r)
        assert d.decision is Decision.HUMAN_REVIEW
        assert d.reason_code is ReasonCode.ORDER_BLOCKED


# ──────────────────── Confidence dampener semantics ──────────────────


class TestCompletionConfidence:
    """Confidence may withhold a resolution. It may never grant one."""

    def test_low_confidence_blocks_an_otherwise_clean_resolution(self) -> None:
        call = make_call(confidence=0.41, confidence_label="low")
        d = run(call=call)
        assert d.decision is Decision.HUMAN_REVIEW
        assert d.reason_code is ReasonCode.LOW_COMPLETION_CONFIDENCE

    def test_confidence_at_threshold_is_permitted(self) -> None:
        call = make_call(confidence=0.70)
        assert run(call=call).decision is Decision.RESOLVE_ON_TIME

    def test_task_not_completed_blocks_resolution(self) -> None:
        assert run(call=make_call(task_completed=False)).decision is Decision.HUMAN_REVIEW

    def test_null_task_completed_blocks_resolution(self) -> None:
        assert run(call=make_call(task_completed=None)).decision is Decision.HUMAN_REVIEW

    def test_absent_confidence_blocks_resolution(self) -> None:
        assert run(call=make_call(confidence=None)).decision is Decision.HUMAN_REVIEW

    def test_high_confidence_cannot_rescue_ambiguous_evidence(self) -> None:
        # The dampener is one-directional.
        r = make_recipient(result=make_result(received="unknown", po_status="unknown"))
        d = run(recipient=r, call=make_call(recipients=(r,), confidence=0.99))
        assert d.decision is Decision.HUMAN_REVIEW
        assert d.reason_code is ReasonCode.EVIDENCE_AMBIGUOUS

    def test_high_confidence_cannot_rescue_an_unreached_recipient(self) -> None:
        r = make_recipient(status="failed", result=make_result())
        d = run(
            recipient=r,
            call=make_call(recipients=(r,), confidence=1.0),
            exception=make_exception(attempt_count=3),
        )
        assert d.decision is Decision.HUMAN_REVIEW


# ───────────────────────── Retry / budget ────────────────────────────


class TestRetryBudget:
    def test_not_received_retries_while_budget_remains(self) -> None:
        r = make_recipient(result=make_result(received="no", po_status="unknown"))
        d = run(recipient=r, exception=make_exception(attempt_count=1))
        assert d.decision is Decision.RETRY
        assert d.reason_code is ReasonCode.NOT_RECEIVED

    def test_not_received_escalates_once_budget_is_exhausted(self) -> None:
        r = make_recipient(result=make_result(received="no", po_status="unknown"))
        d = run(recipient=r, exception=make_exception(attempt_count=3))
        assert d.decision is Decision.HUMAN_REVIEW

    def test_max_attempts_prevents_another_call_after_failure(self) -> None:
        call = make_call(status="failed")
        d = run(call=call, exception=make_exception(attempt_count=3))
        assert d.decision is Decision.HUMAN_REVIEW

    def test_backoff_pending_stays_in_retry_rather_than_escalating(self) -> None:
        # Attempt 2 requires a 15-minute wait and only 5 have passed.
        # That is a scheduling delay, not an exhausted budget: the
        # workflow must wait in RETRY_PENDING, not land on a human's
        # queue as a false alarm.
        exc = make_exception(attempt_count=1, last_attempt_at=NOW - timedelta(minutes=5))
        r = make_recipient(result=make_result(received="no", po_status="unknown"))
        d = run(recipient=r, exception=exc)
        assert d.decision is Decision.RETRY
        assert DECISION_TO_STATE[d.decision] is ExceptionState.RETRY_PENDING

    def test_backoff_schedule_matches_the_reliability_doc(self) -> None:
        # reliability-and-failure.md §8: attempt 1 immediate, attempt 2
        # after a policy delay, attempt 3 a final bounded attempt.
        s = make_settings()
        assert s.backoff_minutes_for(1) == 0
        assert s.backoff_minutes_for(2) == 15
        assert s.backoff_minutes_for(3) == 60
        # Total beyond the schedule rather than raising.
        assert s.backoff_minutes_for(9) == 60


# ──────────────────────── Structural guarantees ──────────────────────


class TestEngineIsTotalAndAuditable:
    def test_every_decision_carries_a_reason_code_and_text(self) -> None:
        cases = [
            run(),
            run(recipient=make_recipient(result=None), reconciliation_available=True),
            run(call=make_call(status="failed")),
            run(exception=make_exception(state=ExceptionState.HUMAN_REVIEW)),
            run(recipient=make_recipient(status="failed")),
        ]
        for d in cases:
            assert isinstance(d.reason_code, ReasonCode)
            assert d.reason_text.strip()
            assert d.input_snapshot["exception_id"]

    def test_decision_to_state_covers_every_decision(self) -> None:
        for decision in Decision:
            assert decision in DECISION_TO_STATE, decision

    def test_engine_is_deterministic(self) -> None:
        results = {run().decision for _ in range(50)}
        assert results == {Decision.RESOLVE_ON_TIME}

    def test_no_resolution_state_is_reachable_without_validated_evidence(self) -> None:
        # Sweep every structurally-broken result shape and assert none of
        # them can produce a resolved state.
        broken = [None, {}, {"received": "yes"}, "not-an-object", 42]
        for raw in broken:
            d = run(
                recipient=make_recipient(result=raw),  # type: ignore[arg-type]
                exception=make_exception(attempt_count=3),
            )
            assert DECISION_TO_STATE[d.decision] not in (
                ExceptionState.RESOLVED_ON_TIME,
                ExceptionState.RESOLVED_DELAYED,
            ), raw


class TestOutcomeClassification:
    @pytest.mark.parametrize(
        ("result", "expected"),
        [
            (make_result(), Outcome.CONFIRMED_ON_TIME),
            (make_result(po_status="delayed"), Outcome.CONFIRMED_DELAYED),
            (make_result(needs_human="yes"), Outcome.BLOCKED_NEED_HUMAN),
            (make_result(po_status="blocked"), Outcome.BLOCKED_NEED_HUMAN),
            (make_result(received="unknown", po_status="unknown"), Outcome.ANSWER_UNCLEAR),
        ],
    )
    def test_outcomes(self, result: dict[str, str], expected: Outcome) -> None:
        v = validate_recipient_result(make_recipient(result=result))
        assert v.ok
        assert v.evidence is not None
        assert classify_outcome(v.evidence) is expected
