"""SQLite access with plain SQL. Every write that touches several rows is one transaction."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import date
from pathlib import Path

from .errors import DuplicateReceiptError
from .prices import Observation, effective_unit_prices
from .verify import Line, VerificationResult

SCHEMA = Path(__file__).with_name("schema.sql")


def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA.read_text(encoding="utf-8"))
    return conn


def store_key(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def _store_id(conn, name: str | None) -> int | None:
    if not name:
        return None
    key = store_key(name)
    conn.execute("INSERT OR IGNORE INTO stores(name, name_key) VALUES (?, ?)", (name.strip(), key))
    return conn.execute("SELECT id FROM stores WHERE name_key = ?", (key,)).fetchone()[0]


def find_by_sha(conn, sha256: str) -> int | None:
    row = conn.execute("SELECT id FROM receipts WHERE image_sha256 = ?", (sha256,)).fetchone()
    return row[0] if row else None


def _insert_lines(conn, receipt_id: int, lines: list[Line]) -> None:
    conn.executemany(
        """INSERT INTO line_items(receipt_id, line_no, raw_text, description, kind, section, quantity,
                                  unit, unit_price_cents, line_total_cents, applies_to_line)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        [(receipt_id, l.line_no, l.raw_text, l.description, l.kind, l.section, l.quantity, l.unit,
          l.unit_price_cents, l.line_total_cents, l.applies_to_line) for l in lines],
    )


def _receipt_values(result: VerificationResult) -> dict:
    r = result.receipt
    return dict(
        store_name_raw=r.store_name,
        purchase_date=r.purchase_date.isoformat() if r.purchase_date else None,
        date_raw=r.date_raw, currency=r.currency, subtotal_cents=r.subtotal_cents,
        tax_cents=r.tax_cents, total_cents=r.total_cents, legibility=r.legibility,
        status=result.status, review_reasons=json.dumps([i.to_dict() for i in result.issues]),
    )


def save_receipt(conn, *, sha256: str, redacted_path: str | None, result: VerificationResult,
                 model: str, prompt_version: str, extraction_json: str) -> int:
    """Insert a receipt and all its lines atomically. Raises DuplicateReceiptError."""
    if (existing := find_by_sha(conn, sha256)) is not None:
        raise DuplicateReceiptError(existing)
    v = _receipt_values(result)
    try:
        with conn:  # one transaction: commit on success, roll back on any exception
            cur = conn.execute(
                """INSERT INTO receipts(image_sha256, redacted_path, store_id, store_name_raw, purchase_date,
                       date_raw, currency, subtotal_cents, tax_cents, total_cents, legibility, status,
                       review_reasons, model, prompt_version, extraction_json)
                   VALUES (:sha, :path, :store_id, :store_name_raw, :purchase_date, :date_raw, :currency,
                       :subtotal_cents, :tax_cents, :total_cents, :legibility, :status, :review_reasons,
                       :model, :prompt_version, :extraction_json)""",
                dict(v, sha=sha256, path=redacted_path, store_id=_store_id(conn, v["store_name_raw"]),
                     model=model, prompt_version=prompt_version, extraction_json=extraction_json),
            )
            receipt_id = cur.lastrowid
            _insert_lines(conn, receipt_id, result.receipt.lines)
    except sqlite3.IntegrityError as e:
        # Lost a race with another writer on the same image.
        if "image_sha256" in str(e) and (existing := find_by_sha(conn, sha256)) is not None:
            raise DuplicateReceiptError(existing) from e
        raise
    return receipt_id


def replace_extraction(conn, receipt_id: int, result: VerificationResult, extraction_json: str) -> None:
    """Apply a human-corrected extraction: re-verified result replaces fields and lines atomically."""
    v = _receipt_values(result)
    with conn:
        conn.execute("DELETE FROM line_items WHERE receipt_id = ?", (receipt_id,))
        conn.execute(
            """UPDATE receipts SET store_id=:store_id, store_name_raw=:store_name_raw,
                   purchase_date=:purchase_date, date_raw=:date_raw, currency=:currency,
                   subtotal_cents=:subtotal_cents, tax_cents=:tax_cents, total_cents=:total_cents,
                   legibility=:legibility, status=:status, review_reasons=:review_reasons,
                   extraction_json=:extraction_json, reviewed_at=datetime('now')
               WHERE id=:id""",
            dict(v, id=receipt_id, store_id=_store_id(conn, v["store_name_raw"]),
                 extraction_json=extraction_json),
        )
        _insert_lines(conn, receipt_id, result.receipt.lines)


def set_status(conn, receipt_id: int, status: str, note: str | None = None) -> None:
    with conn:
        row = conn.execute("SELECT review_reasons FROM receipts WHERE id = ?", (receipt_id,)).fetchone()
        if row is None:
            raise KeyError(receipt_id)
        reasons = json.loads(row[0])
        if note:
            reasons.append({"code": "HUMAN", "message": note, "blocking": False})
        conn.execute(
            "UPDATE receipts SET status=?, review_reasons=?, reviewed_at=datetime('now') WHERE id=?",
            (status, json.dumps(reasons), receipt_id),
        )


def get_receipt(conn, receipt_id: int):
    return conn.execute("SELECT * FROM receipts WHERE id = ?", (receipt_id,)).fetchone()


def get_lines(conn, receipt_id: int) -> list[Line]:
    rows = conn.execute("SELECT * FROM line_items WHERE receipt_id = ? ORDER BY line_no", (receipt_id,))
    return [Line(r["line_no"], r["raw_text"], r["description"], r["kind"], r["section"], r["quantity"],
                 r["unit"], r["unit_price_cents"], r["line_total_cents"], r["applies_to_line"]) for r in rows]


def list_receipts(conn, status: str | None = None):
    sql = """SELECT r.id, r.status, r.purchase_date, r.total_cents, r.review_reasons,
                    COALESCE(s.name, r.store_name_raw) AS store
             FROM receipts r LEFT JOIN stores s ON s.id = r.store_id"""
    args = ()
    if status:
        sql += " WHERE r.status = ?"
        args = (status,)
    return conn.execute(sql + " ORDER BY r.id", args).fetchall()


def observations(conn, query: str) -> list[Observation]:
    """Price observations for items whose description contains `query`, VERIFIED receipts only."""
    receipts = conn.execute(
        """SELECT DISTINCT r.id, s.name AS store, r.purchase_date
           FROM receipts r JOIN stores s ON s.id = r.store_id
           JOIN line_items li ON li.receipt_id = r.id
           WHERE r.status = 'verified' AND r.purchase_date IS NOT NULL
             AND li.kind = 'item' AND li.description LIKE ?""",
        (f"%{query}%",),
    ).fetchall()
    q = query.lower()
    out = []
    for r in receipts:
        for line, price in effective_unit_prices(get_lines(conn, r["id"])):
            if line.description and q in line.description.lower():
                out.append(Observation(r["store"], line.description, line.unit or "each",
                                       date.fromisoformat(r["purchase_date"]), price))
    return out
