"""CORD-v2 ground truth -> our comparable form, and scoring of one extraction against it.

Quirks handled (from the dataset, see tests):
- `menu` is a single object OR a list of objects
- prices have inconsistent separators and prefixes: "16,500", "16.500", "Rp 16.500", "Rp. 16,500.00"
- `nm` (name) is sometimes a list of strings (name split over lines)
- `sub_total` may be missing entirely (then only the total is scored)
Scored fields: item name, count, price (line total), subtotal, total. CORD has no store/date labels.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from pricelens.money import MoneyParseError, parse_amount

NAME_MATCH = 0.8  # name similarity counted as "correct" (OCR-style near misses like 'GANACHE' vs 'GANACHF' fail)


def as_list(x) -> list:
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def idr(value) -> int | None:
    """CORD price string -> whole rupiah; None if absent or unparseable."""
    if value is None:
        return None
    if isinstance(value, list):
        value = " ".join(map(str, value))
    s = re.sub(r"(?i)\bRP\.?", "", str(value))   # "Rp." first: its dot is not a separator
    s = re.sub(r"[^\d.,()\-]", "", s)              # then spaces, "@", "x"
    s = s.strip(".,")
    if not s:
        return None
    try:
        return parse_amount(s, "IDR")
    except MoneyParseError:
        return None


def count(value) -> float | None:
    if value is None:
        return None
    m = re.search(r"\d+(?:[.,]\d+)?", str(value))
    return float(m.group(0).replace(",", ".")) if m else None


def name(value) -> str:
    if isinstance(value, list):
        value = " ".join(map(str, value))
    return re.sub(r"\s+", " ", str(value or "")).strip().upper()


@dataclass
class TruthItem:
    name: str
    count: float | None
    price: int | None


@dataclass
class Truth:
    items: list[TruthItem]
    subtotal: int | None
    total: int | None


def load_truth(gt_parse: dict) -> Truth:
    items = [TruthItem(name(m.get("nm")), count(m.get("cnt")), idr(m.get("price")))
             for m in as_list(gt_parse.get("menu")) if isinstance(m, dict) and m.get("nm")]
    sub = gt_parse.get("sub_total") or {}
    tot = gt_parse.get("total") or {}
    if isinstance(sub, list):
        sub = sub[0] if sub else {}
    if isinstance(tot, list):
        tot = tot[0] if tot else {}
    return Truth(items, idr(sub.get("subtotal_price")), idr(tot.get("total_price")))


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


@dataclass
class ReceiptScore:
    items_expected: int = 0
    names_ok: int = 0
    counts_ok: int = 0
    counts_scored: int = 0
    prices_ok: int = 0
    prices_scored: int = 0
    subtotal_ok: bool | None = None   # None = not labeled in CORD
    total_ok: bool | None = None
    extra_lines: int = 0              # extracted item lines that matched nothing
    failures: list[str] = field(default_factory=list)


def score(truth: Truth, lines: list[dict], subtotal: int | None, total: int | None) -> ReceiptScore:
    """`lines`: extracted item lines as {'name', 'count', 'price'} (price = line total, whole rupiah)."""
    s = ReceiptScore(items_expected=len(truth.items))
    # Greedy matching: best name similarity, a matching price as tie-breaker (+0.2).
    pairs = []
    for i, t in enumerate(truth.items):
        for j, l in enumerate(lines):
            sim = similarity(t.name, name(l["name"]))
            if sim >= 0.5:
                pairs.append((sim + (0.2 if t.price is not None and l["price"] == t.price else 0), i, j, sim))
    used_t, used_l = set(), set()
    for _, i, j, sim in sorted(pairs, reverse=True):
        if i in used_t or j in used_l:
            continue
        used_t.add(i), used_l.add(j)
        t, l = truth.items[i], lines[j]
        if sim >= NAME_MATCH:
            s.names_ok += 1
        else:
            s.failures.append(f"name: expected {t.name!r}, got {name(l['name'])!r}")
        if t.count is not None:
            s.counts_scored += 1
            got = l["count"] if l["count"] is not None else 1.0  # unprinted count means 1
            if abs(got - t.count) < 1e-6:
                s.counts_ok += 1
            else:
                s.failures.append(f"count of {t.name!r}: expected {t.count:g}, got {got:g}")
        if t.price is not None:
            s.prices_scored += 1
            if l["price"] == t.price:
                s.prices_ok += 1
            else:
                s.failures.append(f"price of {t.name!r}: expected {t.price}, got {l['price']}")
    for i, t in enumerate(truth.items):
        if i not in used_t:
            s.failures.append(f"missing item {t.name!r} ({t.price})")
            if t.price is not None:
                s.prices_scored += 1
            if t.count is not None:
                s.counts_scored += 1
    s.extra_lines = len(lines) - len(used_l)
    if truth.subtotal is not None:
        s.subtotal_ok = subtotal == truth.subtotal
        if not s.subtotal_ok:
            s.failures.append(f"subtotal: expected {truth.subtotal}, got {subtotal}")
    if truth.total is not None:
        s.total_ok = total == truth.total
        if not s.total_ok:
            s.failures.append(f"total: expected {truth.total}, got {total}")
    return s
