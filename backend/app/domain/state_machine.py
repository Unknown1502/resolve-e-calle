"""Pure transition rules for exceptions and workflow runs.

No database, no clock, no I/O. Persistence applies these decisions under
an optimistic version check (``app.db.repositories``); this module only
answers "is this move legal?".

Two properties are enforced here rather than trusted to callers:

1. **Terminal states have no outbound edges.** A late CALL-E result
   arriving after an operator resolved an exception by hand cannot move
   it. The attempt raises :class:`TerminalStateProtected`, which callers
   treat as benign and record as historical evidence.
2. **No state transitions to itself.** A replayed webhook must be a
   no-op, not a version bump.
"""

from __future__ import annotations

from typing import TypeAlias

from app.domain.enums import (
    TERMINAL_EXCEPTION_STATES,
    TERMINAL_RUN_STATES,
    ExceptionState,
    WorkflowRunState,
)
from app.domain.errors import InvalidTransition, TerminalStateProtected

#: Legal exception transitions, keyed by source state.
#:
#: Read this table against ``docs/hackathon-build/prd.md`` §5 -- it is
#: the executable form of that diagram.
_EXCEPTION_EDGES: dict[ExceptionState, frozenset[ExceptionState]] = {
    ExceptionState.OPEN: frozenset(
        {
            ExceptionState.ELIGIBLE,
            # Eligibility can refuse outright (blocklist, no phone).
            ExceptionState.HUMAN_REVIEW,
            ExceptionState.CLOSED_UNRESOLVED,
        }
    ),
    ExceptionState.ELIGIBLE: frozenset(
        {
            ExceptionState.CALL_PLANNED,
            ExceptionState.HUMAN_REVIEW,
            ExceptionState.CLOSED_UNRESOLVED,
        }
    ),
    ExceptionState.CALL_PLANNED: frozenset(
        {
            ExceptionState.CALLING,
            # Attempt budget can be exhausted between planning and dispatch.
            ExceptionState.HUMAN_REVIEW,
            ExceptionState.CLOSED_UNRESOLVED,
        }
    ),
    ExceptionState.CALLING: frozenset(
        {
            ExceptionState.RESULT_RECEIVED,
            # Webhook late, lost, or ambiguous -- go ask the provider.
            ExceptionState.RECONCILING,
            # Provider failed terminally with no result to validate.
            ExceptionState.RETRY_PENDING,
            ExceptionState.HUMAN_REVIEW,
        }
    ),
    ExceptionState.RECONCILING: frozenset(
        {
            # Authoritative provider state confirmed a terminal call.
            ExceptionState.RESULT_RECEIVED,
            # Provider says still running -- go back and keep waiting.
            ExceptionState.CALLING,
            ExceptionState.RETRY_PENDING,
            ExceptionState.HUMAN_REVIEW,
        }
    ),
    ExceptionState.RESULT_RECEIVED: frozenset(
        {
            ExceptionState.EVIDENCE_VALIDATED,
            # Result was null or failed re-validation.
            ExceptionState.RETRY_PENDING,
            ExceptionState.HUMAN_REVIEW,
        }
    ),
    ExceptionState.EVIDENCE_VALIDATED: frozenset(
        {
            ExceptionState.RESOLVED_ON_TIME,
            ExceptionState.RESOLVED_DELAYED,
            ExceptionState.RETRY_PENDING,
            ExceptionState.HUMAN_REVIEW,
            ExceptionState.CLOSED_UNRESOLVED,
        }
    ),
    ExceptionState.RETRY_PENDING: frozenset(
        {
            ExceptionState.CALL_PLANNED,
            ExceptionState.HUMAN_REVIEW,
            ExceptionState.CLOSED_UNRESOLVED,
        }
    ),
}

_RUN_EDGES: dict[WorkflowRunState, frozenset[WorkflowRunState]] = {
    WorkflowRunState.PLANNED: frozenset(
        {WorkflowRunState.CALL_PLANNED, WorkflowRunState.ABANDONED}
    ),
    WorkflowRunState.CALL_PLANNED: frozenset(
        {WorkflowRunState.CALLING, WorkflowRunState.COMPLETED, WorkflowRunState.ABANDONED}
    ),
    WorkflowRunState.CALLING: frozenset(
        {WorkflowRunState.RESULT_RECEIVED, WorkflowRunState.ABANDONED}
    ),
    WorkflowRunState.RESULT_RECEIVED: frozenset(
        {
            # A retry re-plans a call within the same run.
            WorkflowRunState.CALL_PLANNED,
            WorkflowRunState.COMPLETED,
            WorkflowRunState.ABANDONED,
        }
    ),
}

_TERMINALS: frozenset[ExceptionState | WorkflowRunState] = frozenset(
    TERMINAL_EXCEPTION_STATES | TERMINAL_RUN_STATES
)

#: Either state family. ``TypeAlias`` rather than 3.12 ``type`` syntax,
#: because the project targets Python 3.11.
State: TypeAlias = "ExceptionState | WorkflowRunState"


def _edges_for(state: State) -> dict[State, frozenset[State]]:
    if isinstance(state, ExceptionState):
        return _EXCEPTION_EDGES  # type: ignore[return-value]
    if isinstance(state, WorkflowRunState):
        return _RUN_EDGES  # type: ignore[return-value]
    raise InvalidTransition(f"unknown state type: {type(state).__name__}")


def is_terminal(state: State) -> bool:
    """True when no outbound transition exists from ``state``."""
    return state in _TERMINALS


def next_states(state: State) -> frozenset[State]:
    """Every state reachable from ``state`` in one legal move."""
    return _edges_for(state).get(state, frozenset())


def can_transition(src: State, dst: State) -> bool:
    """Non-raising legality check.

    Returns ``False`` -- rather than raising -- for cross-family moves,
    so that callers probing legality do not have to guard the call.
    """
    if type(src) is not type(dst):
        return False
    return dst in next_states(src)


def assert_can_transition(src: State, dst: State) -> None:
    """Raise unless ``src -> dst`` is legal.

    Raises:
        TerminalStateProtected: ``src`` is terminal. Expected and benign;
            record the incoming result as evidence and leave state alone.
        InvalidTransition: the move is illegal for any other reason,
            which indicates a bug in the caller.
    """
    if is_terminal(src):
        raise TerminalStateProtected(
            f"{src} is terminal; refusing to move it to {dst}",
            context={"from": str(src), "to": str(dst)},
        )
    if type(src) is not type(dst):
        raise InvalidTransition(
            f"cannot transition across state families: "
            f"{type(src).__name__}.{src} -> {type(dst).__name__}.{dst}"
        )
    if dst not in next_states(src):
        raise InvalidTransition(
            f"illegal transition {src} -> {dst}; "
            f"legal targets are {sorted(str(s) for s in next_states(src))}",
            context={"from": str(src), "to": str(dst)},
        )
