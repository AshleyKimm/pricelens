from datetime import date

import pytest

from pricelens.dates import parse_receipt_date
from pricelens.money import MoneyParseError, format_amount, parse_amount, parse_cents


@pytest.mark.parametrize("raw,cents", [
    ("3.49", 349), ("$3.49", 349), ("1,234.50", 123450), ("3.5", 350), ("12", 1200),
    ("0.50-", -50), ("-0.50", -50), ("(0.50)", -50), ("1.00CR", -100), ("$-1.00", -100),
    ("1.234,50", 123450), (" 3.49 ", 349), (None, None), ("", None),
])
def test_usd(raw, cents):
    assert parse_cents(raw) == cents


@pytest.mark.parametrize("raw,amount", [
    ("25.000", 25000), ("25,000", 25000), ("Rp 25.000", 25000), ("Rp. 25,000", 25000),
    ("RP25.000", 25000), ("1.250.000", 1250000), ("25.000,00", 25000), ("-5.000", -5000), ("500", 500),
])
def test_idr_has_no_minor_unit(raw, amount):
    assert parse_amount(raw, "IDR") == amount


@pytest.mark.parametrize("raw", ["abc", "1.2.3", "12,34,567", "$", "3.49x", "1..00"])
def test_rejects_garbage(raw):
    with pytest.raises(MoneyParseError):
        parse_cents(raw)


def test_idr_rejects_nonzero_fraction():
    with pytest.raises(MoneyParseError):
        parse_amount("25.50", "IDR")


def test_format():
    assert format_amount(-50) == "-$0.50"
    assert format_amount(123450) == "$1,234.50"
    assert format_amount(25000, "IDR") == "IDR 25,000"


@pytest.mark.parametrize("raw,expected,ambiguous", [
    ("2026-11-18 14:02", date(2026, 11, 18), False),
    ("11/18/2026", date(2026, 11, 18), False),       # 18 > 12, so unambiguous
    ("18/11/26", date(2026, 11, 18), False),
    ("03/04/26", date(2026, 3, 4), True),            # ambiguous, US locale -> March 4
    ("04/04/26", date(2026, 4, 4), False),           # same either way
    ("11-18-26 02:14PM", date(2026, 11, 18), False),
    ("NOV 18, 2026", date(2026, 11, 18), False),
    ("Nov. 18 26", date(2026, 11, 18), False),
    ("18 NOV 2026", date(2026, 11, 18), False),
    ("18-Nov-2026", date(2026, 11, 18), False),
    ("DATE: 2026/11/18", date(2026, 11, 18), False),
])
def test_dates(raw, expected, ambiguous):
    p = parse_receipt_date(raw)
    assert p.value == expected and p.ambiguous == ambiguous


def test_ambiguous_date_other_locale():
    assert parse_receipt_date("03/04/26", locale="EU").value == date(2026, 4, 3)


@pytest.mark.parametrize("raw", [None, "", "no date here", "13/13/26", "02/30/2026"])
def test_unparseable_dates(raw):
    assert parse_receipt_date(raw) is None
