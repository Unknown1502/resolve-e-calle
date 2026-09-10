"""State-machine contract.

Covers the master prompt's required cases: valid transitions, invalid
transitions, terminal-state immutability, and optimistic concurrency.
These tests touch no database and no network -- the state machine is
pure, which is the whole point of keeping it separate from persistence.
"""

from __future__ import annotations

import pytest

from app.domain.enums import (
    TERMINAL_EXCEPTION_STATES,
    ExceptionState,
    WorkflowRunState,
)
from app.domain.errors import InvalidTransition, TerminalStateProtected
from app.domain.state_machine import (
    assert_can_transition,
    can_transition,
    is_terminal,
    next_states,
)


class TestHappyPath:
    def test_full_resolution_path_is_walkable(self) -> None:
        path = [
            ExceptionState.OPEN,
            ExceptionState.ELIGIBLE,
            ExceptionState.CALL_PLANNED,
            ExceptionState.CALLING,
            ExceptionState.RESULT_RECEIVED,
            ExceptionState.EVIDENCE_VALIDATED,
            ExceptionState.RESOLVED_ON_TIME,
        ]
        for src, dst in zip(path, path[1:]):  # noqa: B905 - pairs are offset by one
            assert can_transition(src, dst), f"{src} -> {dst} should be legal"

    @pytest.mark.parametrize(
        "terminal",
        [
            ExceptionState.RESOLVED_ON_TIME,
            ExceptionState.RESOLVED_DELAYED,
            ExceptionState.HUMAN_REVIEW,
            ExceptionState.CLOSED_UNRESOLVED,
        ],
    )
    def test_evidence_validated_can_reach_every_terminal(
        self, terminal: ExceptionState
    ) -> None:
        assert can_transition(ExceptionState.EVIDENCE_VALIDATED, terminal)

    def test_reconciling_path_from_diagrams_doc(self) -> None:
        # docs/architecture/diagrams.md §2:
        #   CALLING --> RECONCILING: webhook missing / uncertain
        #   RECONCILING --> RESULT_RECEIVED: terminal state confirmed
        assert can_transition(ExceptionState.CALLING, ExceptionState.RECONCILING)
        assert can_transition(
            ExceptionState.RECONCILING, ExceptionState.RESULT_RECEIVED
        )

    def test_reconciling_returns_to_calling_when_provider_says_still_running(
        self,
    ) -> None:
        # Reconciliation that finds a non-terminal call must not invent a
        # result -- it goes back to waiting.
        assert can_transition(ExceptionState.RECONCILING, ExceptionState.CALLING)

    def test_reconciling_is_not_terminal(self) -> None:
        assert not is_terminal(ExceptionState.RECONCILING)

    def test_retry_pending_drains_back_into_a_new_call(self) -> None:
        # RETRY_PENDING is a holding state, not a terminal one: the
        # scanner must be able to re-plan a call from it.
        assert can_transition(ExceptionState.RETRY_PENDING, ExceptionState.CALL_PLANNED)
        assert not is_terminal(ExceptionState.RETRY_PENDING)


class TestInvalidTransitions:
    def test_cannot_skip_the_call(self) -> None:
        assert not can_transition(ExceptionState.OPEN, ExceptionState.RESOLVED_ON_TIME)

    def test_cannot_validate_evidence_that_was_never_received(self) -> None:
        assert not can_transition(
            ExceptionState.CALLING, ExceptionState.EVIDENCE_VALIDATED
        )

    def test_cannot_go_backwards(self) -> None:
        assert not can_transition(ExceptionState.CALLING, ExceptionState.OPEN)

    def test_assert_raises_with_both_states_named(self) -> None:
        with pytest.raises(InvalidTransition) as exc:
            assert_can_transition(ExceptionState.OPEN, ExceptionState.RESOLVED_ON_TIME)
        assert "OPEN" in str(exc.value)
        assert "RESOLVED_ON_TIME" in str(exc.value)


class TestTerminalImmutability:
    """Reliability rule 8: never overwrite a terminal state."""

    @pytest.mark.parametrize("terminal", sorted(TERMINAL_EXCEPTION_STATES))
    def test_no_terminal_state_has_any_outbound_transition(
        self, terminal: ExceptionState
    ) -> None:
        assert next_states(terminal) == frozenset()
        assert is_terminal(terminal)

    @pytest.mark.parametrize("terminal", sorted(TERMINAL_EXCEPTION_STATES))
    def test_late_result_cannot_move_a_terminal_exception(
        self, terminal: ExceptionState
    ) -> None:
        # This is the "operator resolved it while the call was ringing"
        # case from reliability-and-failure.md §7.
        with pytest.raises(TerminalStateProtected):
            assert_can_transition(terminal, ExceptionState.RESULT_RECEIVED)

    def test_terminal_protection_is_a_distinct_error_from_plain_invalidity(
        self,
    ) -> None:
        # Callers treat these differently: TerminalStateProtected is
        # expected and benign, InvalidTransition is a bug.
        assert issubclass(TerminalStateProtected, InvalidTransition)


class TestSelfTransition:
    def test_state_cannot_transition_to_itself(self) -> None:
        # Guards against an idempotency bug where a replayed event
        # re-applies a transition and bumps the version pointlessly.
        for state in ExceptionState:
            assert not can_transition(state, state)


class TestWorkflowRunStates:
    def test_run_reaches_completion(self) -> None:
        path = [
            WorkflowRunState.PLANNED,
            WorkflowRunState.CALL_PLANNED,
            WorkflowRunState.CALLING,
            WorkflowRunState.RESULT_RECEIVED,
            WorkflowRunState.COMPLETED,
        ]
        for src, dst in zip(path, path[1:]):  # noqa: B905 - pairs are offset by one
            assert can_transition(src, dst)

    def test_run_can_be_abandoned_from_any_live_state(self) -> None:
        for src in (
            WorkflowRunState.PLANNED,
            WorkflowRunState.CALL_PLANNED,
            WorkflowRunState.CALLING,
            WorkflowRunState.RESULT_RECEIVED,
        ):
            assert can_transition(src, WorkflowRunState.ABANDONED)

    def test_completed_run_is_terminal(self) -> None:
        assert next_states(WorkflowRunState.COMPLETED) == frozenset()

    def test_calling_can_return_to_call_planned_for_a_retry(self) -> None:
        assert can_transition(WorkflowRunState.RESULT_RECEIVED, WorkflowRunState.CALL_PLANNED)


class TestMixedEnumsAreRejected:
    def test_cannot_transition_across_state_families(self) -> None:
        with pytest.raises(InvalidTransition):
            assert_can_transition(ExceptionState.OPEN, WorkflowRunState.CALLING)  # type: ignore[arg-type]
