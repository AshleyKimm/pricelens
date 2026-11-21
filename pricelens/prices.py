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
    description: str        # product: the normalized name if labeled (Layer 2), else the printed name
    unit: str
    day: date
    unit_price_cents: int   # effective price per unit, after item-level discounts
    printed: str = ""       # printed name at this store, for display
    category: str | None = None
    variety: str = ""       # Layer 2: 'jasmine', 'whole'... part of a price series, shown in comparisons
    pack: str = ""          # pack size as printed ('24 count', '16 oz'); part of a price series
    base_unit: str | None = None     # 'count' / 'g' / 'ml' if the unit price is known
    base_price: float | None = None  # minor units per base unit (e.g. per egg, per gram)


def latest_by_store(observations: list[Observation]) -> dict[tuple[str, str], list[Observation]]:
    """For cross-store comparison: (product, unit) -> latest observation per store, cheapest first."""
    latest: dict[tuple, Observation] = {}
    for o in observations:
        k = (o.description, o.unit, o.store, o.variety, o.pack)
        if k not in latest or o.day > latest[k].day:
            latest[k] = o
    out: dict[tuple[str, str], list[Observation]] = {}
    for (product, unit, *_), o in latest.items():
        out.setdefault((product, unit), []).append(o)
    for v in out.values():
        v.sort(key=comparison_key(v))
    return out


def comparable_by_unit_price(rows: list[Observation]) -> bool:
    """Unit prices are comparable only if every row has one, in the same base unit."""
    return bool(rows) and all(o.base_price is not None for o in rows) and len({o.base_unit for o in rows}) == 1


def comparison_key(rows: list[Observation]):
    """Cheapest-first order: by unit price when comparable, otherwise by package price."""
    if comparable_by_unit_price(rows):
        return lambda o: (o.base_price, o.unit_price_cents)
    return lambda o: o.unit_price_cents


@dataclass(frozen=True)
class PriceChange:
    store: str
    description: str
    unit: str
    from_day: date
    to_day: date
    from_cents: int
    to_cents: int
    variety: str = ""
    pack: str = ""

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
    # Whole vs 2% milk and a 12-pack vs an 18-pack are separate series: switching isn't a price change.
    key = lambda o: (o.store, o.description, o.unit, o.variety, o.pack)
    changes = []
    for k, group in groupby(sorted(observations, key=lambda o: (key(o), o.day)), key=key):
        per_day: dict[date, int] = {}
        for o in group:
            per_day[o.day] = min(per_day.get(o.day, o.unit_price_cents), o.unit_price_cents)
        days = sorted(per_day)
        for a, b in zip(days, days[1:]):
            if per_day[a] != per_day[b]:
                changes.append(PriceChange(*k[:3], a, b, per_day[a], per_day[b], variety=k[3], pack=k[4]))
    return changes
