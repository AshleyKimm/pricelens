"""Price history and price-change logic (pure functions; the DB layer only feeds them rows).

Layer 1 identifies a product by (store, description, unit): printed text is stable within a
store. Cross-store comparison needs Layer 2's normalized names.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from itertools import groupby

from .verify import Line


@dataclass(frozen=True)
class Observation:
    store: str
    description: str
    unit: str
    day: date
    unit_price_cents: int  # effective price per unit, after item-level discounts


@dataclass(frozen=True)
class PriceChange:
    store: str
    description: str
    unit: str
    from_day: date
    to_day: date
    from_cents: int
    to_cents: int

    @property
    def change_cents(self) -> int:
        return self.to_cents - self.from_cents

    @property
    def change_pct(self) -> float:
        return 100.0 * self.change_cents / self.from_cents if self.from_cents else 0.0


def effective_unit_prices(lines: list[Line]) -> list[tuple[Line, int]]:
    """For each item line: (line, price per unit after its own discounts), rounded to the cent.

    Coupons/refunds/fees are excluded: receipt-level coupons can't be fairly assigned to one
    item, and refunds aren't purchases.
    """
    discounts: dict[int, int] = {}
    for l in lines:
        if l.kind == "discount" and l.applies_to_line and l.line_total_cents is not None:
            discounts[l.applies_to_line] = discounts.get(l.applies_to_line, 0) + l.line_total_cents
    out = []
    for l in lines:
        if l.kind != "item" or l.line_total_cents is None:
            continue
        qty = l.quantity if l.quantity else 1
        net = l.line_total_cents + discounts.get(l.line_no, 0)
        out.append((l, round(net / qty)))
    return out


def price_changes(observations: list[Observation]) -> list[PriceChange]:
    """Consecutive price changes per (store, description, unit).

    Several observations on one day collapse to the lowest price (e.g. one bought on sale).
    Unchanged prices produce no PriceChange.
    """
    key = lambda o: (o.store, o.description, o.unit)
    changes = []
    for k, group in groupby(sorted(observations, key=lambda o: (key(o), o.day)), key=key):
        per_day: dict[date, int] = {}
        for o in group:
            per_day[o.day] = min(per_day.get(o.day, o.unit_price_cents), o.unit_price_cents)
        days = sorted(per_day)
        for a, b in zip(days, days[1:]):
            if per_day[a] != per_day[b]:
                changes.append(PriceChange(*k, a, b, per_day[a], per_day[b]))
    return changes
