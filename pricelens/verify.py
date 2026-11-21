"""Code-based verification of model output. No AI here.

normalize(): model strings -> integer cents / dates (parse problems become issues).
check():     rules V1-V8 + S1 on the normalized receipt.
verify():    both, plus the resulting status.

Any *blocking* issue -> status 'needs_review'. Non-blocking issues are notes (e.g. an
ambiguous date resolved by locale) that are stored but don't stop the receipt.
"""
from __future__ import annotations

import math
from decimal import ROUND_HALF_UP, Decimal
import re
from dataclasses import dataclass, field
from datetime import date

from .dates import parse_receipt_date
from .models import ExtractedReceipt
from .money import MoneyParseError, format_amount, parse_amount

NEGATIVE_KINDS = {"discount", "coupon", "refund"}
MAX_AGE_YEARS = 5
# Decided 2026-11-21: the app accepts US and Canadian dollars only. Other currencies are flagged (V9)
# and kept out of prices. (Money parsing still supports others, e.g. IDR for the CORD eval.)
SUPPORTED_CURRENCIES = {"USD", "CAD"}


@dataclass(frozen=True)
class Issue:
    code: str
    message: str
    blocking: bool = True

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "blocking": self.blocking}


@dataclass
class Line:
    line_no: int
    raw_text: str
    description: str | None
    kind: str
    section: str
    quantity: float | None
    unit: str | None
    unit_price_cents: int | None
    line_total_cents: int | None
    applies_to_line: int | None = None


@dataclass
class Receipt:
    store_name: str | None
    date_raw: str | None
    purchase_date: date | None
    currency: str
    subtotal_cents: int | None
    tax_cents: int
    total_cents: int | None
    legibility: str
    lines: list[Line] = field(default_factory=list)
    model_date_iso: str | None = None
    date_ambiguous: bool = False


@dataclass
class VerificationResult:
    receipt: Receipt
    issues: list[Issue]

    @property
    def status(self) -> str:
        return "needs_review" if any(i.blocking for i in self.issues) else "verified"


def _money(value, where: str, issues: list[Issue], currency: str) -> int | None:
    try:
        return parse_amount(value, currency)
    except MoneyParseError:
        issues.append(Issue("V6", f"{where}: unreadable amount {value!r}"))
        return None


def normalize(ext: ExtractedReceipt, locale: str = "US") -> tuple[Receipt, list[Issue]]:
    issues: list[Issue] = []
    cur = ext.currency
    lines = []
    for i, el in enumerate(ext.lines, start=1):
        total = _money(el.line_total, f"line {i}", issues, cur)
        unit_price = _money(el.unit_price, f"line {i} unit price", issues, cur)
        # Sign convention is enforced in code, not trusted to the model: reductions are negative.
        if total is not None and el.kind in NEGATIVE_KINDS:
            total = -abs(total)
        elif total is not None and el.kind in ("item", "fee"):
            if total < 0:
                issues.append(Issue("V4", f"line {i}: {el.kind} has a negative amount"))
        if unit_price is not None:
            unit_price = abs(unit_price)
        lines.append(
            Line(i, el.raw_text, el.description, el.kind, el.section,
                 abs(el.quantity) if el.quantity is not None else None,
                 el.unit, unit_price, total, el.applies_to_line)
        )

    tax = 0
    for t in ext.tax_lines:
        c = _money(t, "tax", issues, cur)
        tax += c or 0

    parsed = parse_receipt_date(ext.date_raw, locale)
    rec = Receipt(
        store_name=(ext.store_name or "").strip() or None,
        date_raw=ext.date_raw,
        purchase_date=parsed.value if parsed else None,
        currency=ext.currency,
        subtotal_cents=_money(ext.subtotal, "subtotal", issues, cur),
        tax_cents=tax,
        total_cents=_money(ext.total, "total", issues, cur),
        legibility=ext.legibility,
        lines=lines,
        model_date_iso=ext.date_iso,
        date_ambiguous=bool(parsed and parsed.ambiguous),
    )
    return rec, issues


def _sum(lines) -> int:
    return sum(l.line_total_cents for l in lines)


WEIGHED_UNITS = {"lb", "kg", "oz", "g"}


def line_tolerance_cents(quantity: float, unit: str | None = None) -> int:
    """Printed unit prices are rounded to the cent, so qty x unit_price can be off by up to
    qty x 0.5 cents (e.g. '3 for $5' prints 1.67 each: 3 x 1.67 = 5.01). Weighed items also get
    1 cent, because the printed weight may itself be rounded (1.504 lb printed as 1.50)."""
    base = math.floor(abs(quantity) * 0.5 + 1e-9)
    return max(base, 1) if unit in WEIGHED_UNITS else base


def expected_line_total(quantity: float, unit_price: int) -> int:
    """qty x unit price, rounded HALF UP like a register (Python's round() is banker's: 88.5 -> 88)."""
    return int((Decimal(str(quantity)) * unit_price).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _looks_like_card_number(digits: str) -> bool:
    if not 15 <= len(digits) <= 19:
        return False
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


_SENSITIVE_KEYWORDS = re.compile(r"\b(ACCT|ACCOUNT|CARD\s*#|MEMBER\s*#|MEMBER\s*ID|LOYALTY|REWARDS?\s*#)", re.I)


def find_sensitive_text(text: str) -> bool:
    """S1: a Luhn-valid 15-19 digit run (card number) or an account/loyalty label with a long number.
    UPCs (12 digits) and EANs (13) are deliberately not matched."""
    for run in re.findall(r"\d[\d -]{13,}\d", text):
        if _looks_like_card_number(re.sub(r"\D", "", run)):
            return True
    return bool(_SENSITIVE_KEYWORDS.search(text) and re.search(r"\d{6,}", text))


def scrub_text(text: str) -> str:
    """Replace whatever find_sensitive_text() would match with [REDACTED], so it is never stored."""
    def card(m):
        return "[REDACTED]" if _looks_like_card_number(re.sub(r"\D", "", m[0])) else m[0]

    text = re.sub(r"\d[\d -]{13,}\d", card, text)
    if _SENSITIVE_KEYWORDS.search(text):
        text = re.sub(r"\d{6,}", "[REDACTED]", text)
    return text


def check(rec: Receipt, today: date) -> list[Issue]:
    issues: list[Issue] = []
    fmt = lambda amount: format_amount(amount, rec.currency)

    # V9 currency
    if (rec.currency or "").upper() not in SUPPORTED_CURRENCIES:
        issues.append(Issue("V9", f"currency {rec.currency!r} is not supported (only USD and CAD)"))

    # V6 required fields
    if not rec.store_name:
        issues.append(Issue("V6", "store name missing"))
    if rec.total_cents is None:
        issues.append(Issue("V6", "total missing"))
    if not any(l.kind == "item" for l in rec.lines):
        issues.append(Issue("V6", "no item lines"))
    missing = [l.line_no for l in rec.lines if l.line_total_cents is None]
    if missing:
        issues.append(Issue("V6", f"unreadable line totals on lines {missing}"))

    # V7 legibility
    if rec.legibility == "poor":
        issues.append(Issue("V7", "model reported poor legibility"))

    items = [l for l in rec.lines if l.section == "items"]
    after = [l for l in rec.lines if l.section == "after_subtotal"]
    sums_ok = not missing

    # V1 / V2 / V3 totals
    if sums_ok and rec.subtotal_cents is not None:
        s = _sum(items)
        if s != rec.subtotal_cents:
            issues.append(Issue("V1", f"items sum to {fmt(s)} but printed subtotal is {fmt(rec.subtotal_cents)} "
                                      f"(diff {fmt(rec.subtotal_cents - s)})"))
        if rec.total_cents is not None:
            expected = rec.subtotal_cents + rec.tax_cents + _sum(after)
            if expected != rec.total_cents:
                issues.append(Issue("V2", f"subtotal + tax + adjustments = {fmt(expected)} but printed total is "
                                          f"{fmt(rec.total_cents)} (diff {fmt(rec.total_cents - expected)})"))
    elif sums_ok and rec.total_cents is not None:
        issues.append(Issue("V3", "no subtotal printed; checked items + tax = total instead", blocking=False))
        expected = _sum(items) + rec.tax_cents + _sum(after)
        if expected != rec.total_cents:
            issues.append(Issue("V3", f"items + tax + adjustments = {fmt(expected)} but printed total is "
                                      f"{fmt(rec.total_cents)} (diff {fmt(rec.total_cents - expected)})"))

    # V4 per-line arithmetic
    for l in rec.lines:
        if l.kind in ("item", "refund") and None not in (l.quantity, l.unit_price_cents, l.line_total_cents):
            expected = expected_line_total(l.quantity, l.unit_price_cents)
            diff = abs(abs(l.line_total_cents) - expected)
            if diff > line_tolerance_cents(l.quantity, l.unit):
                issues.append(Issue("V4", f"line {l.line_no}: {l.quantity} x {fmt(l.unit_price_cents)} = "
                                          f"{fmt(expected)} but line total is {fmt(abs(l.line_total_cents))}"))

    # V8 discount links
    by_no = {l.line_no: l for l in rec.lines}
    for l in rec.lines:
        if l.kind == "discount":
            parent = by_no.get(l.applies_to_line) if l.applies_to_line else None
            if parent is None or parent.kind != "item":
                issues.append(Issue("V8", f"line {l.line_no}: discount doesn't point at an item line"))

    # V5 date
    if rec.purchase_date is None:
        issues.append(Issue("V5", f"could not parse date {rec.date_raw!r}"))
    else:
        if rec.model_date_iso and rec.model_date_iso != rec.purchase_date.isoformat():
            issues.append(Issue("V5", f"model read date as {rec.model_date_iso}, code parsed "
                                      f"{rec.date_raw!r} as {rec.purchase_date.isoformat()}"))
        if rec.date_ambiguous:
            issues.append(Issue("V5", f"ambiguous date {rec.date_raw!r} read as "
                                      f"{rec.purchase_date.isoformat()} by locale", blocking=False))
        if rec.purchase_date > today:
            issues.append(Issue("V5", f"date {rec.purchase_date} is in the future"))
        elif (today - rec.purchase_date).days > 366 * MAX_AGE_YEARS:
            issues.append(Issue("V5", f"date {rec.purchase_date} is more than {MAX_AGE_YEARS} years old"))

    # S1 sensitive numbers leaked into model output (redaction backstop)
    texts = [rec.store_name or ""] + [l.raw_text for l in rec.lines] + [l.description or "" for l in rec.lines]
    if any(find_sensitive_text(t) for t in texts):
        issues.append(Issue("S1", "output contains what looks like a card or loyalty number"))

    return issues


def verify(ext: ExtractedReceipt, today: date | None = None, locale: str = "US") -> VerificationResult:
    rec, issues = normalize(ext, locale)
    issues += check(rec, today or date.today())
    return VerificationResult(rec, issues)
