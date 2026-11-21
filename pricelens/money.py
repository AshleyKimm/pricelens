"""Convert printed money strings to integer minor units (cents for USD, whole rupiah for IDR).

The model returns money as the string printed on the receipt ("3.49", "0.50-", "Rp 25.000").
Converting in code (not in the model) means rounding, separators and signs are handled
deterministically and unit-tested, and we never store floats.
"""
from __future__ import annotations

import re

# Number of minor-unit digits per currency (ISO 4217). Unknown currencies default to 2.
MINOR_DIGITS = {"IDR": 0, "JPY": 0, "KRW": 0, "VND": 0, "CLP": 0, "HUF": 2, "USD": 2, "EUR": 2, "GBP": 2, "CAD": 2}

_PREFIX = re.compile(r"^(RP\.?|IDR|USD|US\$|\$|€|£|¥|₩)")


class MoneyParseError(ValueError):
    pass


def minor_digits(currency: str | None) -> int:
    return MINOR_DIGITS.get((currency or "USD").upper(), 2)


def parse_amount(value: str | None, currency: str | None = "USD") -> int | None:
    """Printed amount -> integer minor units.

    USD: '3.49' -> 349, '$1,234.50' -> 123450, '1.234,50' -> 123450 (EU style)
    IDR: '25.000' -> 25000, 'Rp 25,000' -> 25000, '25.000,00' -> 25000
    Negative markers: leading '-', trailing '-', '(...)', trailing 'CR'.
    """
    if value is None:
        return None
    s = value.strip().upper().replace(" ", "")
    if s == "":
        return None
    negative = False
    if s.startswith("(") and s.endswith(")"):
        negative, s = True, s[1:-1]
    if s.endswith("CR"):
        negative, s = True, s[:-2]
    if s.startswith("-"):
        negative, s = True, s[1:]
    s = _PREFIX.sub("", s)
    if s.startswith("-"):  # "$-1.00"
        negative, s = True, s[1:]
    if s.endswith("-"):
        negative, s = True, s[:-1]
    if not s or not re.fullmatch(r"[\d.,]+", s) or not s[0].isdigit() or not s[-1].isdigit():
        raise MoneyParseError(f"not a money amount: {value!r}")

    digits = minor_digits(currency)
    # The last separator is a decimal point only if 1-2 digits follow it;
    # a separator followed by 3 digits is a thousands separator.
    m = re.search(r"[.,](\d+)$", s)
    if m and len(m[1]) <= 2:
        whole, frac = s[: m.start()], m[1]
    else:
        whole, frac = s, ""
    if re.search(r"[.,]", whole) and not re.fullmatch(r"\d{1,3}([.,]\d{3})+", whole):
        raise MoneyParseError(f"bad thousands grouping: {value!r}")
    whole = re.sub(r"[.,]", "", whole)

    if digits == 0:
        if frac and int(frac) != 0:
            raise MoneyParseError(f"{currency} has no minor unit: {value!r}")
        amount = int(whole)
    else:
        amount = int(whole) * 10**digits + int((frac or "0").ljust(digits, "0")[:digits])
    return -amount if negative else amount


def parse_cents(value: str | None) -> int | None:
    """USD shorthand kept for readability in tests."""
    return parse_amount(value, "USD")


def format_amount(amount: int | None, currency: str | None = "USD") -> str:
    if amount is None:
        return "?"
    sign = "-" if amount < 0 else ""
    a = abs(amount)
    digits = minor_digits(currency)
    cur = (currency or "USD").upper()
    if digits == 0:
        return f"{sign}{cur} {a:,}"
    sym = "$" if cur == "USD" else f"{cur} "
    return f"{sign}{sym}{a // 10**digits:,}.{a % 10**digits:0{digits}d}"
