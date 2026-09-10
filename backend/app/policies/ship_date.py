"""Deterministic parsing of supplier-stated ship dates.

``ship_date`` arrives as free text because it comes out of a phone
conversation: "Tuesday", "next Friday", "the 15th", "unknown". The
decision engine needs ``valid_ship_date(result.ship_date)`` and
``delay_within_policy(...)`` to be *decidable*, and the product
principle is that unknown is a first-class result.

So this module has one rule:

    **Parse confidently, or return None. Never guess.**

Returning ``None`` is not a failure -- it routes the exception to human
review with reason ``SHIP_DATE_UNUSABLE``, which is the correct outcome.
The dangerous behaviour would be resolving a purchase order against a
date we inferred.

Ambiguous numeric forms are deliberately rejected. ``09/11`` is 9
November to most of the world and 11 September in the US; the supplier
who said it meant one of them, and we cannot know which. A two-month
error on a delivery commitment is worse than an escalation.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta

#: Tokens that explicitly mean "no date was given". Matched after
#: normalisation, and treated identically to unparseable input.
_UNKNOWN_TOKENS = frozenset(
    {"", "unknown", "unclear", "none", "n/a", "na", "tbd", "not sure", "no date"}
)

_WEEKDAYS: dict[str, int] = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "thur": 3,
    "thurs": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}

_MONTHS: dict[str, int] = {
    "january": 1, "jan": 1,
    "february": 2, "feb": 2,
    "march": 3, "mar": 3,
    "april": 4, "apr": 4,
    "may": 5,
    "june": 6, "jun": 6,
    "july": 7, "jul": 7,
    "august": 8, "aug": 8,
    "september": 9, "sep": 9, "sept": 9,
    "october": 10, "oct": 10,
    "november": 11, "nov": 11,
    "december": 12, "dec": 12,
}

_ISO_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_MONTH_DAY_RE = re.compile(
    r"^([a-z]+)\.?\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?$"
)
_DAY_MONTH_RE = re.compile(
    r"^(\d{1,2})(?:st|nd|rd|th)?\s+(?:of\s+)?([a-z]+)\.?(?:,?\s*(\d{4}))?$"
)
#: Purely numeric slash/dot forms -- rejected on purpose. See module docstring.
_AMBIGUOUS_NUMERIC_RE = re.compile(r"^\d{1,2}[/.\-]\d{1,2}(?:[/.\-]\d{2,4})?$")


def _normalise(raw: str) -> str:
    text = raw.strip().lower()
    text = re.sub(r"[‘’“”]", "", text)
    # Drop conversational lead-ins the model may have preserved verbatim.
    text = re.sub(
        r"^(?:on|by|around|about|probably|maybe|should be|we'?ll ship|ship(?:ping)? (?:on|by)?)\s+",
        "",
        text,
    )
    return re.sub(r"\s+", " ", text).strip(" .,")


def _next_weekday(reference: date, weekday: int, *, skip_a_week: bool) -> date:
    """The next occurrence of ``weekday`` strictly after ``reference``."""
    delta = (weekday - reference.weekday()) % 7
    if delta == 0:
        delta = 7  # "Friday" said on a Friday means the coming Friday.
    if skip_a_week:
        delta += 7
    return reference + timedelta(days=delta)


def parse_ship_date(raw: str | None, *, reference: date | None = None) -> date | None:
    """Return a concrete date, or ``None`` when the text is not decidable.

    Args:
        raw: Supplier-stated text straight from the structured result.
        reference: "Today" for relative expressions. Defaults to the
            current UTC date. Injected in tests so results are stable.

    Returns:
        A :class:`datetime.date`, or ``None`` when the input is unknown,
        ambiguous, or unparseable. ``None`` always routes to human
        review; it never resolves a workflow.
    """
    if raw is None:
        return None

    today = reference or datetime.now(UTC).date()
    text = _normalise(raw)

    if text in _UNKNOWN_TOKENS:
        return None

    # Ambiguous numeric forms are refused before anything else tries to
    # be clever about them.
    if _AMBIGUOUS_NUMERIC_RE.match(text):
        return None

    if m := _ISO_RE.match(text):
        try:
            return date(int(m[1]), int(m[2]), int(m[3]))
        except ValueError:
            return None

    if text in ("today",):
        return today
    if text in ("tomorrow", "tmrw"):
        return today + timedelta(days=1)

    # "next tuesday" / "this friday" / "tuesday"
    relative = text
    skip_a_week = False
    for prefix, skip in (("next ", True), ("this coming ", False), ("this ", False)):
        if relative.startswith(prefix):
            relative = relative[len(prefix) :]
            skip_a_week = skip
            break
    if relative in ("week", "monday week"):
        return None
    if (weekday := _WEEKDAYS.get(relative)) is not None:
        return _next_weekday(today, weekday, skip_a_week=skip_a_week)

    # "september 15" / "sept 15 2026" / "15 september"
    for pattern, month_idx, day_idx in ((_MONTH_DAY_RE, 1, 2), (_DAY_MONTH_RE, 2, 1)):
        if m := pattern.match(text):
            month = _MONTHS.get(m[month_idx])
            if month is None:
                continue
            day = int(m[day_idx])
            year = int(m[3]) if m[3] else today.year
            try:
                parsed = date(year, month, day)
            except ValueError:
                return None
            # A bare month/day that already passed means next year --
            # but only just past, otherwise we are guessing.
            if not m[3] and parsed < today - timedelta(days=30):
                try:
                    parsed = date(year + 1, month, day)
                except ValueError:
                    return None
            return parsed

    return None


def delay_within_policy(
    ship_date: date,
    ack_due_at: datetime,
    delay_threshold_hours: int,
) -> bool:
    """True when a stated ship date is a delay the policy tolerates.

    ``docs/prompts/decision-engine.md`` gates ``RESOLVE_DELAYED`` on this:
    a delay inside the threshold resolves automatically, a longer one is
    a commercial judgment and goes to a human.
    """
    due_date = ack_due_at.date()
    slip = ship_date - due_date
    return slip <= timedelta(hours=delay_threshold_hours)
