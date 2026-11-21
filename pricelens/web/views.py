"""Data for each page, as plain functions over a DB connection (testable without HTTP)."""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from .. import db
from ..prices import Observation, latest_by_store, price_changes
from .chart import MAX_SLOTS, Series, legend_html, line_chart_svg


@dataclass
class ProductRow:
    name: str
    category: str | None
    varieties: list[str]
    stores: int
    cheapest: Observation | None
    cheapest_at: list[Observation] = field(default_factory=list)  # every store tied at the lowest price


def search(conn, q: str) -> list[ProductRow]:
    obs = db.observations(conn, q.strip())
    by_product: dict[str, list[Observation]] = defaultdict(list)
    for o in obs:
        by_product[o.description].append(o)
    rows = []
    for name, items in by_product.items():
        latest = [o for group in latest_by_store(items).values() for o in group]
        cats = Counter(o.category for o in items if o.category)
        best = min(latest, key=lambda o: o.unit_price_cents) if latest else None
        # Ties only count within the same unit: $0.59/lb and $0.59/each aren't the same price.
        tied = sorted((o for o in latest if best and o.unit == best.unit
                       and o.unit_price_cents == best.unit_price_cents), key=lambda o: o.store)
        rows.append(ProductRow(
            name=name,
            category=cats.most_common(1)[0][0] if cats else None,
            varieties=sorted({o.variety for o in items if o.variety}),
            stores=len({o.store for o in items}),
            cheapest=best,
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
    unit: str
    chart_svg: str
    legend: str
    other_units: list[str] = field(default_factory=list)


def product(conn, name: str) -> ProductPage | None:
    obs = [o for o in db.observations(conn, name) if o.description == name]
    if not obs:
        return None
    units = Counter(o.unit for o in obs)
    unit = units.most_common(1)[0][0]
    main = [o for o in obs if o.unit == unit]
    slots = store_slots(conn)
    # One series per store (+ variety when a store sells several), colored by store.
    groups: dict[tuple[str, str], list[Observation]] = defaultdict(list)
    for o in main:
        groups[(o.store, o.variety)].append(o)
    multi_var = {s for s in {k[0] for k in groups} if sum(1 for k in groups if k[0] == s) > 1}
    series = []
    for (store, variety), items in sorted(groups.items(), key=lambda kv: (slots.get(kv[0][0], 99), kv[0][1])):
        label = f"{store} ({variety})" if store in multi_var and variety else store
        per_day: dict = {}
        for o in items:  # same-day duplicates: lowest price, as in price_changes()
            per_day[o.day] = min(per_day.get(o.day, o.unit_price_cents), o.unit_price_cents)
        series.append(Series(label, slots.get(store, 1), sorted(per_day.items())))
    comparison = sorted((o for g in latest_by_store(main).values() for o in g), key=lambda o: o.unit_price_cents)
    return ProductPage(
        name=name,
        history=sorted(obs, key=lambda o: (o.day, o.store)),
        comparison=comparison,
        changes=price_changes(main),
        unit=unit,
        chart_svg=line_chart_svg(series, title=f"Price of {name} per {unit} over time"),
        legend=legend_html(series),
        other_units=[u for u in units if u != unit],
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
