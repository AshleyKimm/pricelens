"""Layer 2 service: categorize receipts with a cache, respect the data policy, apply human corrections."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .categories import CATEGORIES, clean_name, description_key, name_problems, review_reasons
from .units import to_base
from .categorize import Categorizer, ItemToLabel
from .errors import AuthError, PriceLensError


MAX_BATCH = 60  # items per request; keeps each response small


@dataclass
class CategorizeSummary:
    receipt_id: int                      # first receipt in the group
    receipt_ids: list[int] = field(default_factory=list)
    store: str | None = None
    new_labels: int = 0
    cached: int = 0
    flagged: int = 0
    calls: int = 0
    skipped: str | None = None
    error: str | None = None
    flagged_items: list[str] = field(default_factory=list)


def _skip_reason(conn, receipt_id: int, categorizer) -> tuple[str | None, sqlite3.Row | None]:
    r = conn.execute("""SELECT r.status, r.source, r.store_id, s.name AS store FROM receipts r
                        LEFT JOIN stores s ON s.id = r.store_id WHERE r.id = ?""", (receipt_id,)).fetchone()
    if r is None:
        return "no such receipt", None
    if r["status"] == "rejected":
        return "receipt rejected", r
    if r["store_id"] is None:
        return "receipt has no store", r
    if getattr(categorizer, "trains_on_input", False) and r["source"] != "public":
        return "private receipt: this provider tier may train on inputs", r
    return None, r


def _label_store(conn, store_id: int, store: str, receipt_ids: list[int], categorizer, s: CategorizeSummary) -> None:
    """All uncached items from these receipts (one store) -> as few requests as possible."""
    marks = ",".join("?" * len(receipt_ids))
    lines = conn.execute(f"""SELECT description_key, description, raw_text FROM line_items
                             WHERE receipt_id IN ({marks}) AND kind IN ('item', 'refund')
                               AND description_key IS NOT NULL ORDER BY receipt_id, line_no""",
                         receipt_ids).fetchall()
    known = {row[0] for row in conn.execute("SELECT description_key FROM item_labels WHERE store_id = ?", (store_id,))}
    todo: dict[str, tuple[str, str]] = {}
    for l in lines:
        if l["description_key"] in known:
            s.cached += 1
        elif l["description_key"] not in todo:
            todo[l["description_key"]] = (l["description"] or l["description_key"], l["raw_text"])
    keys = list(todo.keys())
    for start in range(0, len(keys), MAX_BATCH):
        chunk = keys[start:start + MAX_BATCH]
        items = [ItemToLabel(i, todo[k][0], todo[k][1]) for i, k in enumerate(chunk, start=1)]
        # Names already in use anywhere (all stores, humans included) so the model reuses them.
        known_names = [row[0] for row in conn.execute(
            "SELECT DISTINCT normalized_name FROM item_labels WHERE normalized_name IS NOT NULL")]
        s.calls += 1
        outcome = categorizer.label(store, items, known_names)
        with conn:  # one transaction per batch
            for label in outcome.labels:
                key = chunk[label.id - 1]
                reasons = review_reasons(label, printed=todo[key][0])
                # A pack size that failed the printed-text check is kept for audit but not used for prices.
                base = None if any(r.startswith("P1") for r in reasons) else to_base(label.pack_quantity, label.pack_unit)
                cur = conn.execute(
                    """INSERT INTO item_labels(store_id, description_key, description, normalized_name, variety,
                           pack_quantity, pack_unit, pack_base_qty, pack_base_unit,
                           category, confidence, needs_review, review_reasons, source, model, prompt_version)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ai', ?, ?)
                       ON CONFLICT(store_id, description_key) DO NOTHING""",  # never overwrite (esp. human rows)
                    (store_id, key, todo[key][0], clean_name(label.normalized_name) or None,
                     clean_name(label.variety) or None, label.pack_quantity, label.pack_unit,
                     base[0] if base else None, base[1] if base else None, label.category,
                     label.confidence, int(bool(reasons)), json.dumps(reasons), outcome.model,
                     outcome.prompt_version),
                )
                s.new_labels += cur.rowcount
                if reasons:
                    s.flagged += 1
                    s.flagged_items.append(f"{todo[key][0]}: {'; '.join(reasons)}")


def categorize_receipt(conn, receipt_id: int, categorizer: Categorizer) -> CategorizeSummary:
    """Single receipt; errors propagate (the batch version records them instead)."""
    reason, r = _skip_reason(conn, receipt_id, categorizer)
    s = CategorizeSummary(receipt_id, [receipt_id], r["store"] if r else None, skipped=reason)
    if not reason:
        _label_store(conn, r["store_id"], r["store"], [receipt_id], categorizer, s)
    return s


def drop_outdated_ai_labels(conn, prompt_version: str) -> int:
    """Delete AI labels made by an older prompt so they're re-labeled. Human corrections are kept."""
    with conn:
        return conn.execute("DELETE FROM item_labels WHERE source = 'ai' AND COALESCE(prompt_version, '') != ?",
                            (prompt_version,)).rowcount


def categorize_all(conn, categorizer: Categorizer, receipt_ids: list[int] | None = None) -> list[CategorizeSummary]:
    """Groups receipts by store so each store's uncached items go in one request (free tiers allow few
    requests per day). One failing store doesn't stop the rest; an auth failure does."""
    ids = receipt_ids or [row[0] for row in conn.execute("SELECT id FROM receipts ORDER BY id")]
    out: list[CategorizeSummary] = []
    groups: dict[int, tuple[str, list[int]]] = {}
    for rid in ids:
        reason, r = _skip_reason(conn, rid, categorizer)
        if reason:
            out.append(CategorizeSummary(rid, [rid], r["store"] if r else None, skipped=reason))
        else:
            groups.setdefault(r["store_id"], (r["store"], []))[1].append(rid)
    for store_id, (store, rids) in groups.items():
        s = CategorizeSummary(rids[0], rids, store)
        try:
            _label_store(conn, store_id, store, rids, categorizer, s)
        except AuthError:
            raise
        except PriceLensError as e:
            s.error = f"{type(e).__name__}: {e}"
        out.append(s)
    return out


def fix_label(conn, store: str, description: str, normalized_name: str, category: str,
              root: Path | None = None, variety: str = "") -> Path:
    """Human correction: overrides any AI label and is saved as a regression test case.
    Human input goes through the same name checks as model output."""
    if category not in CATEGORIES or category == "uncertain":
        raise ValueError(f"category must be one of: {', '.join(c for c in CATEGORIES if c != 'uncertain')}")
    name, variety = clean_name(normalized_name), clean_name(variety)
    if problems := name_problems(name, category, variety):
        raise ValueError("; ".join(problems))
    srow = conn.execute("SELECT id, name FROM stores WHERE name_key = ?",
                        (" ".join(store.strip().lower().split()),)).fetchone()
    if srow is None:
        raise ValueError(f"unknown store {store!r}")
    key = description_key(description)
    seen = conn.execute("""SELECT MAX(r.source = 'private') FROM line_items li JOIN receipts r ON r.id = li.receipt_id
                           WHERE r.store_id = ? AND li.description_key = ?""", (srow["id"], key)).fetchone()[0]
    if seen is None:
        raise ValueError(f"no item {description!r} on any receipt from {srow['name']}")
    previous = conn.execute("""SELECT normalized_name, variety, category, confidence, source FROM item_labels
                               WHERE store_id = ? AND description_key = ?""", (srow["id"], key)).fetchone()
    with conn:
        conn.execute(
            """INSERT INTO item_labels(store_id, description_key, description, normalized_name, variety, category,
                   confidence, needs_review, review_reasons, source)
               VALUES (?, ?, ?, ?, ?, ?, 'high', 0, '[]', 'human')
               ON CONFLICT(store_id, description_key) DO UPDATE SET normalized_name = excluded.normalized_name,
                   variety = excluded.variety, category = excluded.category, confidence = 'high', needs_review = 0, review_reasons = '[]',
                   source = 'human', model = NULL, prompt_version = NULL, updated_at = datetime('now')""",
            (srow["id"], key, description.strip(), name, variety or None, category),
        )
    # Corrections become test cases. Ones touching private receipts stay out of git.
    root = root or Path.cwd()
    path = root / ("evals/private" if seen else "evals/synthetic") / "label_corrections.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    case = {"store": srow["name"], "description": description.strip(), "expected_name": name,
            "expected_variety": variety, "expected_category": category, "ai_previous": dict(previous) if previous else None,
            "corrected_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(case) + "\n")
    return path


def list_labels(conn, needs_review_only: bool = False):
    sql = """SELECT l.*, s.name AS store FROM item_labels l JOIN stores s ON s.id = l.store_id"""
    if needs_review_only:
        sql += " WHERE l.needs_review = 1"
    return conn.execute(sql + " ORDER BY s.name, l.normalized_name, l.description_key").fetchall()
