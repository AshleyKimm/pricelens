"""Data for each page, as plain functions over a DB connection (testable without HTTP)."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .. import db
from ..money import format_amount
from ..prices import Observation, latest_by_store, price_changes
from ..units import DEFAULT_SYSTEM, display, format_unit_price
from .chart import MAX_SLOTS, Series, legend_html, line_chart_svg


def family(o: Observation) -> str:
    """What a price can be compared against: a unit price per count/g/ml, or else per package of a unit."""
    return o.base_unit if o.base_price is not None else f"package:{o.unit}"


def value(o: Observation, units: str) -> int:
    """The number shown and compared, in minor units: unit price in the chosen system, else package price."""
    if o.base_price is not None:
        return round(display(o.base_price, o.base_unit, units)[0])
    return o.unit_price_cents


def price_text(o: Observation, units: str) -> str:
    if o.base_price is not None:
        return format_unit_price(o.base_price, o.base_unit, units)
    return f"{format_amount(o.unit_price_cents)} / {o.unit}"


def main_family(rows: list[Observation]) -> str:
    """Compare within the most common family, preferring unit prices over per-package prices."""
    counts = Counter(family(o) for o in rows)
    return max(counts, key=lambda f: (not f.startswith("package:"), counts[f]))


@dataclass
class ProductRow:
    name: str
    category: str | None
    varieties: list[str]
    stores: int
    cheapest: Observation | None
    cheapest_text: str = ""
    cheapest_at: list[Observation] = field(default_factory=list)  # every store tied at the lowest price


def search(conn, q: str, units: str = DEFAULT_SYSTEM) -> list[ProductRow]:
    obs = db.observations(conn, q.strip())
    by_product: dict[str, list[Observation]] = defaultdict(list)
    for o in obs:
        by_product[o.description].append(o)
    rows = []
    for name, items in by_product.items():
        latest = [o for group in latest_by_store(items).values() for o in group]
        fam = main_family(latest)
        comparable = [o for o in latest if family(o) == fam]  # $0.59/lb and $0.59/each aren't comparable
        best = min(comparable, key=lambda o: value(o, units)) if comparable else None
        tied = sorted((o for o in comparable if value(o, units) == value(best, units)), key=lambda o: o.store)
        cats = Counter(o.category for o in items if o.category)
        rows.append(ProductRow(
            name=name,
            category=cats.most_common(1)[0][0] if cats else None,
            varieties=sorted({o.variety for o in items if o.variety}),
            stores=len({o.store for o in items}),
            cheapest=best,
            cheapest_text=price_text(best, units) if best else "",
            cheapest_at=tied,
        ))
    return sorted(rows, key=lambda r: (-r.stores, r.name))


def store_slots(conn) -> dict[str, int]:
    """Fixed color slot per store (by store id), so a store keeps its color on every page."""
    return {r["name"]: i % MAX_SLOTS + 1 for i, r in enumerate(conn.execute("SELECT name FROM stores ORDER BY id"))}


@dataclass
class ProductPage:
    name: str
    history: list[Observation]
    comparison: list[Observation]
    changes: list
    by_unit_price: bool          # comparison/chart use unit prices (pack sizes known)
    unit_label: str              # 'each', 'lb', '100 g', or the package unit
    has_winner: bool             # the cheapest row is strictly cheaper than the next
    chart_svg: str
    legend: str
    other_units: list[str] = field(default_factory=list)


def product(conn, name: str, units: str = DEFAULT_SYSTEM) -> ProductPage | None:
    obs = [o for o in db.observations(conn, name) if o.description == name]
    if not obs:
        return None
    fam = main_family(obs)
    main = [o for o in obs if family(o) == fam]
    by_unit = not fam.startswith("package:")
    unit_label = display(1.0, fam, units)[1] if by_unit else fam.split(":", 1)[1]
    slots = store_slots(conn)
    # One series per store (+ variety when a store sells several), colored by store. With unit prices,
    # different pack sizes from one store belong on the same line (that's the point of unit pricing).
    groups: dict[tuple[str, str], list[Observation]] = defaultdict(list)
    for o in main:
        groups[(o.store, o.variety)].append(o)
    multi_var = {s for s in {k[0] for k in groups} if sum(1 for k in groups if k[0] == s) > 1}
    series = []
    for (store, variety), items in sorted(groups.items(), key=lambda kv: (slots.get(kv[0][0], 99), kv[0][1])):
        label = f"{store} ({variety})" if store in multi_var and variety else store
        per_day: dict = {}
        for o in items:  # same-day duplicates: lowest price, as in price_changes()
            v = value(o, units)
            per_day[o.day] = min(per_day.get(o.day, v), v)
        series.append(Series(label, slots.get(store, 1), sorted(per_day.items())))
    comparison = sorted((o for g in latest_by_store(main).values() for o in g), key=lambda o: value(o, units))
    has_winner = len(comparison) > 1 and value(comparison[0], units) < value(comparison[1], units)
    others = sorted({family(o) for o in obs} - {fam})
    return ProductPage(
        name=name,
        history=sorted(obs, key=lambda o: (o.day, o.store)),
        comparison=comparison,
        changes=price_changes(main),
        by_unit_price=by_unit,
        unit_label=unit_label,
        has_winner=has_winner,
        chart_svg=line_chart_svg(series, title=f"Price of {name} per {unit_label} over time"),
        legend=legend_html(series),
        other_units=[f.split(":", 1)[1] + " (no pack size)" if f.startswith("package:") else f for f in others],
    )


def receipts(conn):
    rows = []
    for r in db.list_receipts(conn):
        reasons = json.loads(r["review_reasons"])
        rows.append(dict(r) | {"fails": [i["code"] for i in reasons if i["blocking"]]})
    return rows


def receipt(conn, receipt_id: int):
    r = db.get_receipt(conn, receipt_id)
    if r is None:
        return None
    return {"r": dict(r), "lines": db.get_lines(conn, receipt_id), "issues": json.loads(r["review_reasons"])}


def labels(conn, review_only: bool = False):
    from ..labeling import list_labels
    return [dict(r) | {"reasons": json.loads(r["review_reasons"])} for r in list_labels(conn, review_only)]


def correction_cases(root: Path, conn) -> list[dict]:
    """Saved human corrections and whether the current label still matches them
    (catches a rebuild or bug that loses a correction)."""
    from ..categories import description_key
    out = []
    for path in (root / "evals/synthetic/label_corrections.jsonl", root / "evals/private/label_corrections.jsonl"):
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            case = json.loads(line)
            cur = conn.execute("""SELECT l.normalized_name, l.variety, l.category FROM item_labels l
                                  JOIN stores s ON s.id = l.store_id
                                  WHERE s.name_key = ? AND l.description_key = ?""",
                               (case["store"].lower(), description_key(case["description"]))).fetchone()
            ok = bool(cur) and (cur["normalized_name"], cur["variety"] or "", cur["category"]) == (
                case["expected_name"], case.get("expected_variety", ""), case["expected_category"])
            out.append(case | {"current": dict(cur) if cur else None, "ok": ok})
    # The last correction for an item is the one that counts.
    latest = {(c["store"], c["description"].upper()): c for c in out}
    return list(latest.values())
