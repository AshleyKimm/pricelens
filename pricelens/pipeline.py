"""Add one receipt: hash -> duplicate check -> redact -> confirm -> extract -> verify -> store.

Depends only on the Extractor protocol, so the CLI, tests, demo builder and (Layer 4) batch
runner all share this one code path.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Callable

from . import db
from .errors import DuplicateReceiptError, UnreadableImageError
from .extract import Extractor, save_fixture
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
) -> AddOutcome:
    path = Path(path)
    if not path.is_file():
        raise UnreadableImageError(f"no such image: {path}")
    check_allowed(extractor, path)  # before any processing: real receipts never reach a training tier

    # Duplicate check first: costs nothing, and avoids paying for OCR + an API call.
    sha = sha256_file(path)
    if (existing := db.find_by_sha(conn, sha)) is not None:
        raise DuplicateReceiptError(existing)

    prepared = prepare_fn(path)
    redacted_dir.mkdir(parents=True, exist_ok=True)
    redacted_path = redacted_dir / f"{sha}.jpg"
    redacted_path.write_bytes(prepared.jpeg_bytes)  # exactly the bytes that will be sent

    if confirm is not None and not confirm(prepared, redacted_path):
        raise Cancelled("not sent")

    extraction = extractor.extract(prepared)
    if fixture_dir is not None:
        save_fixture(extraction, sha, fixture_dir)

    result = verify(extraction.receipt, today=today, locale=locale)

    # Backstop for redaction: never store card/loyalty-number-like text, even if the model read it.
    rec = result.receipt
    rec.store_name = scrub_text(rec.store_name) if rec.store_name else rec.store_name
    for line in rec.lines:
        line.raw_text = scrub_text(line.raw_text)
        line.description = scrub_text(line.description) if line.description else line.description

    receipt_id = db.save_receipt(
        conn, sha256=sha, redacted_path=str(redacted_path), result=result,
        model=extraction.model, prompt_version=extraction.prompt_version,
        extraction_json=scrub_text(extraction.raw_json),
        source="public" if is_public_data(path) else "private",  # drives which providers may see its items
    )
    return AddOutcome(receipt_id, result.status, result.issues, result, redacted_path, extraction.model)
