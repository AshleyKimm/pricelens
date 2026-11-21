"""Parse receipt dates independently of the model.

The model also returns `date_iso`; verification (V5) checks that the code's parse of the raw
printed string agrees. Numeric dates like 03/04/26 are ambiguous; we resolve them with a
configured locale and report the ambiguity instead of hiding it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

MONTHS = {
    m: i
    for i, names in enumerate(
        [
            ("JAN", "JANUARY"), ("FEB", "FEBRUARY"), ("MAR", "MARCH"), ("APR", "APRIL"),
            ("MAY",), ("JUN", "JUNE"), ("JUL", "JULY"), ("AUG", "AUGUST"),
            ("SEP", "SEPT", "SEPTEMBER"), ("OCT", "OCTOBER"), ("NOV", "NOVEMBER"),
            ("DEC", "DECEMBER"),
        ],
        start=1,
    )
    for m in names
}
_MONTH_ALT = "|".join(sorted(MONTHS, key=len, reverse=True))

_ISO = re.compile(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b")
_NUMERIC = re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})\b")
# (?!\d) stops "NOV 2026" from being read as day 20 of year 26.
_MON_FIRST = re.compile(rf"\b({_MONTH_ALT})\.?[\s-]*(\d{{1,2}})(?!\d),?[\s-]*(\d{{2}}|\d{{4}})\b")
_DAY_FIRST = re.compile(rf"\b(\d{{1,2}})[\s-]*({_MONTH_ALT})\.?,?[\s-]*(\d{{2}}|\d{{4}})\b")


@dataclass(frozen=True)
class ParsedDate:
    value: date
    ambiguous: bool  # True if day/month order had to be guessed from the locale


def _year(y: str) -> int:
    return 2000 + int(y) if len(y) == 2 else int(y)


def _safe(y: int, m: int, d: int) -> date | None:
    try:
        return date(y, m, d)
    except ValueError:
        return None


def parse_receipt_date(raw: str | None, locale: str = "US") -> ParsedDate | None:
    """Return the first date found in `raw`, or None if nothing parses.

    locale 'US' reads ambiguous numeric dates as month/day, anything else as day/month.
    """
    if not raw:
        return None
    s = raw.upper()

    if m := _ISO.search(s):
        d = _safe(int(m[1]), int(m[2]), int(m[3]))
        return ParsedDate(d, False) if d else None

    if m := _MON_FIRST.search(s):
        d = _safe(_year(m[3]), MONTHS[m[1]], int(m[2]))
        return ParsedDate(d, False) if d else None

    if m := _DAY_FIRST.search(s):
        d = _safe(_year(m[3]), MONTHS[m[2]], int(m[1]))
        return ParsedDate(d, False) if d else None

    if m := _NUMERIC.search(s):
        a, b, y = int(m[1]), int(m[2]), _year(m[3])
        if a > 12 and b <= 12:
            d, amb = _safe(y, b, a), False
        elif b > 12 and a <= 12:
            d, amb = _safe(y, a, b), False
        elif a == b:
            d, amb = _safe(y, a, b), False
        elif locale.upper() == "US":
            d, amb = _safe(y, a, b), True
        else:
            d, amb = _safe(y, b, a), True
        return ParsedDate(d, amb) if d else None

    return None
