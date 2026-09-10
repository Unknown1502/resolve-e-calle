"""Ship-date parsing must be decidable, stable, and never speculative."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from app.policies.ship_date import delay_within_policy, parse_ship_date

# A Wednesday, so weekday arithmetic is unambiguous to a reader.
REF = date(2026, 9, 9)
assert REF.weekday() == 2


class TestUnknownIsFirstClass:
    @pytest.mark.parametrize(
        "raw",
        ["unknown", "Unknown", "  UNKNOWN  ", "", "n/a", "TBD", "not sure", "none", None],
    )
    def test_absent_dates_return_none(self, raw: str | None) -> None:
        assert parse_ship_date(raw, reference=REF) is None

    @pytest.mark.parametrize(
        "raw",
        [
            "sometime next month",
            "when inventory arrives",
            "ask logistics",
            "soon",
            "asap",
            "end of quarter",
        ],
    )
    def test_vague_language_is_not_guessed(self, raw: str) -> None:
        assert parse_ship_date(raw, reference=REF) is None


class TestAmbiguousNumericFormsAreRefused:
    """09/11 is 9 Nov to most of the world and 11 Sep in the US.

    Guessing would silently commit a purchase order to a date two months
    from the one the supplier said. Escalating is strictly better.
    """

    @pytest.mark.parametrize("raw", ["09/11", "9/11/2026", "11-09", "3.4.2026"])
    def test_numeric_day_month_forms_return_none(self, raw: str) -> None:
        assert parse_ship_date(raw, reference=REF) is None


class TestUnambiguousForms:
    def test_iso_date(self) -> None:
        assert parse_ship_date("2026-09-15", reference=REF) == date(2026, 9, 15)

    def test_invalid_iso_date_is_rejected_not_clamped(self) -> None:
        assert parse_ship_date("2026-02-30", reference=REF) is None

    def test_tomorrow(self) -> None:
        assert parse_ship_date("tomorrow", reference=REF) == date(2026, 9, 10)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Friday", date(2026, 9, 11)),
            ("friday", date(2026, 9, 11)),
            ("Fri", date(2026, 9, 11)),
            ("Tuesday", date(2026, 9, 15)),
            ("Monday", date(2026, 9, 14)),
        ],
    )
    def test_weekday_resolves_to_next_occurrence(
        self, raw: str, expected: date
    ) -> None:
        assert parse_ship_date(raw, reference=REF) == expected

    def test_same_weekday_means_next_week_not_today(self) -> None:
        # Said on a Wednesday, "Wednesday" means the coming one.
        assert parse_ship_date("Wednesday", reference=REF) == date(2026, 9, 16)

    def test_next_weekday_skips_a_week(self) -> None:
        assert parse_ship_date("next Tuesday", reference=REF) == date(2026, 9, 22)

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("September 15", date(2026, 9, 15)),
            ("Sept 15", date(2026, 9, 15)),
            ("sep 15 2026", date(2026, 9, 15)),
            ("15 September", date(2026, 9, 15)),
            ("15th of September", date(2026, 9, 15)),
            ("October 1, 2026", date(2026, 10, 1)),
        ],
    )
    def test_month_name_forms(self, raw: str, expected: date) -> None:
        assert parse_ship_date(raw, reference=REF) == expected

    def test_conversational_lead_ins_are_stripped(self) -> None:
        assert parse_ship_date("we'll ship Friday", reference=REF) == date(2026, 9, 11)
        assert parse_ship_date("by Friday", reference=REF) == date(2026, 9, 11)


class TestDelayWithinPolicy:
    ACK_DUE = datetime(2026, 9, 9, 12, 0, tzinfo=UTC)

    def test_ship_on_due_date_is_within_policy(self) -> None:
        assert delay_within_policy(date(2026, 9, 9), self.ACK_DUE, 48)

    def test_two_day_slip_is_exactly_at_the_threshold(self) -> None:
        assert delay_within_policy(date(2026, 9, 11), self.ACK_DUE, 48)

    def test_three_day_slip_exceeds_the_threshold(self) -> None:
        # A delay this long is a commercial judgment, not an automatic
        # resolution -- decision-engine.md sends it to a human.
        assert not delay_within_policy(date(2026, 9, 12), self.ACK_DUE, 48)

    def test_earlier_than_due_is_within_policy(self) -> None:
        assert delay_within_policy(date(2026, 9, 1), self.ACK_DUE, 48)


class TestDeterminism:
    def test_same_input_and_reference_always_yields_same_output(self) -> None:
        results = {parse_ship_date("next Friday", reference=REF) for _ in range(50)}
        assert results == {date(2026, 9, 18)}
