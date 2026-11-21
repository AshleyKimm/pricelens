"""`pricelens verify`: re-check stored records against the rules and the database's own consistency.

It reports; it never fixes anything automatically (a human decides).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from pydantic import ValidationError

from .models import ExtractedReceipt
from .verify import verify


@dataclass
class Finding:
    code: str
    receipt_id: int | None
    message: str


def audit(conn, images_dir: Path | None = None) -> list[Finding]:
    out: list[Finding] = []
    receipts = conn.execute("""SELECT id, status, review_reasons, extraction_json, image_sha256, reviewed_at,
                                      subtotal_cents, total_cents, created_at FROM receipts ORDER BY id""").fetchall()
    for r in receipts:
        rid = r["id"]
        # A1: re-run today's rules on the stored model output (catches rule changes and bad edits).
        try:
            ext = ExtractedReceipt.model_validate_json(r["extraction_json"])
        except ValidationError:
            out.append(Finding("A1", rid, "stored extraction no longer matches the schema"))
        else:
            as_of = date.fromisoformat(r["created_at"][:10])  # judge dates as of when it was added
            now = verify(ext, today=as_of)
            # D1 (possible duplicate) depends on other rows, not on this extraction, so it's left out here.
            stored = sorted(i["code"] for i in json.loads(r["review_reasons"]) if i["blocking"] and i["code"] != "D1")
            now_codes = sorted(i.code for i in now.issues if i.blocking)
            if r["reviewed_at"] is None:  # a human's approve/reject decision overrides the rules on purpose
                if r["status"] == "verified" and now_codes:
                    out.append(Finding("A1", rid, f"stored as verified but the checks now fail: {', '.join(now_codes)}"))
                elif r["status"] == "needs_review" and stored and not now_codes:
                    out.append(Finding("A1", rid, f"flagged ({', '.join(stored)}) but the checks now pass"))
        # A2: stored lines must still add up to the stored subtotal for verified receipts.
        lines = conn.execute("""SELECT COUNT(*) n, SUM(CASE WHEN section = 'items' THEN line_total_cents END) s
                                FROM line_items WHERE receipt_id = ?""", (rid,)).fetchone()
        if lines["n"] == 0:
            out.append(Finding("A2", rid, "receipt has no line items"))
        elif r["status"] == "verified" and r["subtotal_cents"] is not None and lines["s"] != r["subtotal_cents"]:
            out.append(Finding("A2", rid, f"stored lines sum to {lines['s']} but subtotal is {r['subtotal_cents']}"))
        # A3: the image that was sent should still exist (it's the audit trail).
        if images_dir is not None and not (images_dir / f"{r['image_sha256']}.jpg").exists():
            out.append(Finding("A3", rid, "redacted image is missing"))

    # A4: orphans (foreign keys normally prevent these; this catches edits made with FKs off).
    orphans = {
        "line item(s) point at a missing receipt":
            "SELECT COUNT(*) FROM line_items WHERE receipt_id NOT IN (SELECT id FROM receipts)",
        "label(s) point at a missing store":
            "SELECT COUNT(*) FROM item_labels WHERE store_id NOT IN (SELECT id FROM stores)",
    }
    for what, sql in orphans.items():
        if n := conn.execute(sql).fetchone()[0]:
            out.append(Finding("A4", None, f"{n} {what}"))
    # A5: open, retryable ingest failures waiting for `retry-failed`.
    (n,) = conn.execute("SELECT COUNT(DISTINCT image_path) FROM ingest_failures WHERE resolved_at IS NULL AND retryable = 1").fetchone()
    if n:
        out.append(Finding("A5", None, f"{n} image(s) failed earlier and can be retried: pricelens retry-failed"))
    return out
