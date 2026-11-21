"""Add one receipt: hash -> duplicate check -> redact -> confirm -> extract -> verify -> store.

Split in two so a batch can run the slow part in worker threads:
  process()  - no database: redact, extract (the API call), verify, scrub. Safe in any thread.
  store()    - the database write, one transaction. Only the main thread calls it (SQLite: one writer).
add_receipt() = process() + store(), used by the CLI, tests and the demo builder.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

from . import db
from .errors import DuplicateReceiptError, UnreadableImageError
from .extract import ExtractionResult, Extractor, save_fixture
from .money import format_amount
from .policy import check_allowed, is_public_data
from .redact import PreparedImage, prepare, sha256_file
from .verify import Issue, VerificationResult, scrub_text, verify


class Cancelled(Exception):
    """User declined to send the redacted image."""


@dataclass
class AddOutcome:
    receipt_id: int
    status: str
    issues: list[Issue]
    result: VerificationResult
    redacted_path: Path
    model: str


@dataclass
class Processed:
    """Everything needed to store a receipt; produced without touching the database."""
    path: Path
    sha: str
    redacted_path: Path
    extraction: ExtractionResult
    result: VerificationResult
    attempts: int = 1


def process(
    path: Path,
    *,
    sha: str,
    extractor: Extractor,
    redacted_dir: Path,
    confirm: Callable[[PreparedImage, Path], bool] | None = None,
    prepare_fn: Callable[[Path], PreparedImage] = prepare,
    fixture_dir: Path | None = None,
    today: date | None = None,
    locale: str = "US",
    call=None,
) -> Processed:
    """`call(fn) -> (result, attempts)` wraps the API request (a reliability.Caller in batches)."""
    prepared = prepare_fn(path)
    redacted_dir.mkdir(parents=True, exist_ok=True)
    redacted_path = redacted_dir / f"{sha}.jpg"
    redacted_path.write_bytes(prepared.jpeg_bytes)  # exactly the bytes that will be sent

    if confirm is not None and not confirm(prepared, redacted_path):
        raise Cancelled("not sent")

    if call is None:
        extraction, attempts = extractor.extract(prepared), 1
    else:
        extraction, attempts = call(lambda: extractor.extract(prepared))
    if fixture_dir is not None:
        save_fixture(extraction, sha, fixture_dir)

    result = verify(extraction.receipt, today=today, locale=locale)

    # Backstop for redaction: never store card/loyalty-number-like text, even if the model read it.
    rec = result.receipt
    rec.store_name = scrub_text(rec.store_name) if rec.store_name else rec.store_name
    for line in rec.lines:
        line.raw_text = scrub_text(line.raw_text)
        line.description = scrub_text(line.description) if line.description else line.description
    return Processed(path, sha, redacted_path, extraction, result, attempts)


def likely_duplicate(conn, result: VerificationResult) -> int | None:
    """D1: another receipt with the same store, date and total (e.g. two photos of one receipt)."""
    r = result.receipt
    if not (r.store_name and r.purchase_date and r.total_cents is not None):
        return None
    row = conn.execute(
        """SELECT r.id FROM receipts r JOIN stores s ON s.id = r.store_id
           WHERE s.name_key = ? AND r.purchase_date = ? AND r.total_cents = ? AND r.status != 'rejected'
           ORDER BY r.id LIMIT 1""",
        (db.store_key(r.store_name), r.purchase_date.isoformat(), r.total_cents),
    ).fetchone()
    return row[0] if row else None


def store(conn, p: Processed) -> AddOutcome:
    """Main thread only. Flags (never drops or merges) likely duplicates, then saves in one transaction."""
    if (dup := likely_duplicate(conn, p.result)) is not None:
        r = p.result.receipt
        p.result.issues.append(Issue("D1", f"possible duplicate of receipt #{dup}: same store, date and total "
                                           f"({format_amount(r.total_cents, r.currency)})"))
    receipt_id = db.save_receipt(
        conn, sha256=p.sha, redacted_path=str(p.redacted_path), result=p.result,
        model=p.extraction.model, prompt_version=p.extraction.prompt_version,
        extraction_json=scrub_text(p.extraction.raw_json),
        source="public" if is_public_data(p.path) else "private",  # drives which providers may see its items
    )
    return AddOutcome(receipt_id, p.result.status, p.result.issues, p.result, p.redacted_path, p.extraction.model)


def add_receipt(
    path: Path,
    *,
    conn,
    extractor: Extractor,
    redacted_dir: Path,
    confirm: Callable[[PreparedImage, Path], bool] | None = None,
    prepare_fn: Callable[[Path], PreparedImage] = prepare,
    fixture_dir: Path | None = None,
    today: date | None = None,
    locale: str = "US",
    call=None,
) -> AddOutcome:
    path = Path(path)
    if not path.is_file():
        raise UnreadableImageError(f"no such image: {path}")
    check_allowed(extractor, path)  # before any processing: real receipts never reach a training tier

    # Duplicate check first: costs nothing, and avoids paying for OCR + an API call.
    sha = sha256_file(path)
    if (existing := db.find_by_sha(conn, sha)) is not None:
        raise DuplicateReceiptError(existing)

    p = process(path, sha=sha, extractor=extractor, redacted_dir=redacted_dir, confirm=confirm,
                prepare_fn=prepare_fn, fixture_dir=fixture_dir, today=today, locale=locale, call=call)
    return store(conn, p)
