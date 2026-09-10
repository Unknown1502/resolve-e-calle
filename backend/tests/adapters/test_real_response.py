"""Regression tests against a **real** CALL-E response.

`fixtures/real_call_no_answer.json` is not hand-written. It is the
actual body returned by `GET /v1/calls/{id}` on 2026-09-10, for a live
call placed to an authorised number that did not answer
(`call_uFfUVN5yyTTZFSmv2TnxSA`). Both real-response fixtures carry a
personal detail -- the recipient's phone number -- that was replaced
with a placeholder (`+15555550123`) before being committed; every other
field is byte-identical to what the provider returned. See each
fixture's own `_redacted` key for the same note next to the data.

Every other test in this suite proves the system is self-consistent.
These prove it agrees with the provider, which is a different and
stronger claim -- and the only one that could not be made by reading the
OpenAPI document.

The no-answer outcome turned out to be more informative than a clean
success would have been. It exercises the paths that matter most:
an unreached recipient, an all-`unknown` structured result, and -- the
important one -- a **high** completion confidence attached to a call
that did not complete.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.adapters.calle.mapper import map_call
from app.domain.enums import Decision, ReasonCode
from app.policies.evidence_policy import validate_recipient_result
from app.policies.transition_policy import decide
from tests.factories import NOW, make_exception, make_settings

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "real_call_no_answer.json"
CONNECTED_FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "real_call_connected.json"
)


@pytest.fixture(scope="module")
def raw() -> dict[str, Any]:
    if not FIXTURE.is_file():
        pytest.skip(f"real-response fixture not present at {FIXTURE}")
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def connected_raw() -> dict[str, Any]:
    """A second real response: the call that connected (32s, reached),
    captured *before* schema v2 existed. Its `structured_result` has the
    original five fields only -- no `spoke_with`, no `escalation_reason`.
    That makes it real, live evidence for the release-compatibility
    question answered by `TestV1EvidenceCannotBypassV2Identification`
    below: can an old-shaped payload sneak past the new identification
    gate?
    """
    if not CONNECTED_FIXTURE.is_file():
        pytest.skip(f"real-response fixture not present at {CONNECTED_FIXTURE}")
    return json.loads(CONNECTED_FIXTURE.read_text(encoding="utf-8"))


class TestTheProviderAgreesWithOurModel:
    def test_a_real_response_maps_without_loss(self, raw: dict[str, Any]) -> None:
        call = map_call(raw)
        assert call.provider_call_id.startswith("call_")
        assert call.is_terminal
        assert call.status == "failed"
        assert call.recipients, "recipients must survive mapping"

    def test_recipient_ids_are_present_on_retrieve(self, raw: dict[str, Any]) -> None:
        """The single riskiest assumption in the batch design.

        Correlation pins each attempt to a `recipients[].id`. If the
        provider did not return them, batch calling would not work at
        all -- so this is the assertion that de-risked the feature.
        """
        call = map_call(raw)
        for recipient in call.recipients:
            assert recipient.provider_recipient_id.startswith("rcp_")

    def test_our_recipient_schema_was_accepted_and_returned(
        self, raw: dict[str, Any]
    ) -> None:
        """Proof that `recipient_result_schema` round-tripped."""
        result = raw["recipients"][0]["structured_result"]
        assert set(result) == {
            "received",
            "po_status",
            "ship_date",
            "blocker",
            "needs_human",
        }

    def test_the_po_status_rename_was_necessary_and_worked(
        self, raw: dict[str, Any]
    ) -> None:
        """`status` is reserved on recipient results; ours is `po_status`.

        The field came back under the name we sent, and no bare `status`
        key collided with it.
        """
        result = raw["recipients"][0]["structured_result"]
        assert "po_status" in result
        assert "status" not in result

    def test_our_task_level_schema_was_accepted(self, raw: dict[str, Any]) -> None:
        assert raw["structured_result"] == {
            "suppliers_reached": 0,
            "suppliers_confirmed": 0,
        }

    def test_unknown_is_what_the_provider_returns_when_it_cannot_know(
        self, raw: dict[str, Any]
    ) -> None:
        """Nobody answered, so every judgment field came back `unknown`.

        This is the enum-over-boolean decision paying off against the
        real API: a boolean would have had to be false, which reads as
        "the supplier said no".
        """
        result = raw["recipients"][0]["structured_result"]
        assert all(v == "unknown" for v in result.values())


class TestHighConfidenceOnAFailedCall:
    """The case that vindicates treating confidence as a dampener.

    The provider returned `completion_confidence: 0.82 (high)` on a call
    that never connected -- it is confident the task did *not* complete.
    Read as a positive signal, that number would resolve a purchase
    order nobody ever discussed.
    """

    def test_confidence_is_high_while_the_task_did_not_complete(
        self, raw: dict[str, Any]
    ) -> None:
        call = map_call(raw)
        assert call.task_completed is False
        assert call.completion_confidence is not None
        assert call.completion_confidence.score >= 0.70  # above our threshold
        assert call.completion_confidence.label == "high"

    def test_high_confidence_does_not_resolve_anything(
        self, raw: dict[str, Any]
    ) -> None:
        call = map_call(raw)
        decision = decide(
            make_exception(attempt_count=1),
            call,
            call.recipients[0],
            make_settings(),
            now=NOW,
            reconciliation_available=False,
        )
        assert decision.decision is not Decision.RESOLVE_ON_TIME
        assert decision.decision is not Decision.RESOLVE_DELAYED


class TestPolicyOnRealEvidence:
    def test_an_unanswered_call_retries_rather_than_resolving(
        self, raw: dict[str, Any]
    ) -> None:
        call = map_call(raw)
        decision = decide(
            make_exception(attempt_count=1),
            call,
            call.recipients[0],
            make_settings(),
            now=NOW,
            reconciliation_available=False,
        )
        assert decision.decision is Decision.RETRY
        assert decision.reason_code is ReasonCode.PROVIDER_CALL_FAILED

    def test_it_escalates_once_the_budget_is_gone(self, raw: dict[str, Any]) -> None:
        call = map_call(raw)
        decision = decide(
            make_exception(attempt_count=3),
            call,
            call.recipients[0],
            make_settings(),
            now=NOW,
            reconciliation_available=False,
        )
        assert decision.decision is Decision.HUMAN_REVIEW

    def test_the_unreached_recipient_yields_no_evidence(
        self, raw: dict[str, Any]
    ) -> None:
        call = map_call(raw)
        validation = validate_recipient_result(call.recipients[0])
        assert not validation.ok
        assert validation.reason_code is ReasonCode.RECIPIENT_NOT_REACHED

    def test_the_raw_failure_code_is_carried_but_not_branched_on(
        self, raw: dict[str, Any]
    ) -> None:
        import inspect

        from app.policies import transition_policy

        call = map_call(raw)
        assert call.failure_code == "call_failed"
        # Present for display and support; never a branch condition.
        assert "call_failed" not in inspect.getsource(transition_policy)


class TestV1EvidenceCannotBypassV2Identification:
    """Release-compatibility check: can an old-shaped payload resolve?

    `real_call_connected.json` is real, live evidence, not a synthetic
    fixture -- it was captured before `spoke_with`/`escalation_reason`
    existed, so its `structured_result` genuinely has the old five
    fields only. If a payload shaped like this could still produce
    `RESOLVE_ON_TIME` / `RESOLVE_DELAYED`, an old CALL-E response (or a
    stale deployment still requesting the v1 schema) could silently
    bypass the identification and escalation-differentiation gates this
    session added. It cannot: `WIRE_TO_INTERNAL` requires all seven
    fields, so a five-field payload fails structurally before the
    identity gate or any resolve branch is ever reached.
    """

    def test_the_fixture_really_is_v1_shaped(self, connected_raw: dict[str, Any]) -> None:
        # Guard the premise -- if this fixture ever gets migrated to v2
        # shape, this whole test class is checking nothing real.
        result = connected_raw["recipients"][0]["structured_result"]
        assert set(result) == {"received", "po_status", "ship_date", "blocker", "needs_human"}
        assert "spoke_with" not in result
        assert "escalation_reason" not in result

    def test_v1_shaped_evidence_is_rejected_not_silently_upgraded(
        self, connected_raw: dict[str, Any]
    ) -> None:
        call = map_call(connected_raw)
        validation = validate_recipient_result(call.recipients[0])
        assert not validation.ok
        assert validation.reason_code is ReasonCode.STRUCTURED_RESULT_INVALID
        assert "spoke_with" in validation.reason_text
        assert "escalation_reason" in validation.reason_text

    def test_v1_shaped_evidence_cannot_reach_a_resolved_state(
        self, connected_raw: dict[str, Any]
    ) -> None:
        call = map_call(connected_raw)
        decision = decide(
            make_exception(attempt_count=1),
            call,
            call.recipients[0],
            make_settings(),
            now=NOW,
            reconciliation_available=True,
        )
        assert decision.decision not in (Decision.RESOLVE_ON_TIME, Decision.RESOLVE_DELAYED)

    def test_the_rejection_is_structural_not_semantic(
        self, connected_raw: dict[str, Any]
    ) -> None:
        """Confirms *why* this is safe against a tempting wrong fix.

        A permissive alternative would make spoke_with/escalation_reason
        optional with a soft default (e.g. "unknown"), let old evidence
        *validate*, and rely on the identity gate alone to catch it --
        fragile, because a future change to the gate's condition could
        then let a silently-defaulted "unknown" through. The actual
        design refuses one level earlier: `is_structural_failure=True`
        marks this a shape problem, not an ambiguous-but-valid answer,
        so it is indistinguishable from any other malformed payload and
        gets no special-cased leniency.
        """
        call = map_call(connected_raw)
        validation = validate_recipient_result(call.recipients[0])
        assert validation.is_structural_failure is True
