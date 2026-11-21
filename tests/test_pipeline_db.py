"""End-to-end through the pipeline with the FakeExtractor: storage, duplicates, transactions,
flagged receipts excluded from price history."""
import json
import sqlite3
from datetime import date

import pytest
from helpers import line, receipt, receipt_ocr
from PIL import Image

from pricelens import db
from pricelens.errors import DuplicateReceiptError, MalformedOutputError
from pricelens.extract import FakeExtractor, ReplayExtractor, parse_output, save_fixture
from pricelens.models import ExtractedReceipt, strict_json_schema
from pricelens.pipeline import Cancelled, add_receipt
from pricelens.redact import prepare, sha256_file

TODAY = date(2026, 11, 20)
no_ocr = lambda p: prepare(p, ocr=receipt_ocr)  # payment section "found"


@pytest.fixture
def conn():
    return db.connect(":memory:")


def img(tmp_path, name, color):
    p = tmp_path / name
    Image.new("RGB", (100, 200), color).save(p)
    return p


def add(conn, tmp_path, path, extractor, **kw):
    return add_receipt(path, conn=conn, extractor=extractor, redacted_dir=tmp_path / "red",
                       prepare_fn=no_ocr, today=TODAY, **kw)


GOOD = receipt([line("MILK 3.49", "3.49", desc="MILK")], subtotal="3.49", total="3.49")
BAD = receipt([line("MILK 3.49", "3.49", desc="MILK")], subtotal="9.99", total="9.99")


def test_verified_receipt_is_stored_with_lines(conn, tmp_path):
    p = img(tmp_path, "a.png", "white")
    out = add(conn, tmp_path, p, FakeExtractor({sha256_file(p): GOOD}))
    assert out.status == "verified"
    row = db.get_receipt(conn, out.receipt_id)
    assert row["total_cents"] == 349 and row["model"] == "fake"
    assert json.loads(row["extraction_json"])["total"] == "3.49"
    assert [l.raw_text for l in db.get_lines(conn, out.receipt_id)] == ["MILK 3.49"]
    assert out.redacted_path.exists()


def test_flagged_receipt_is_stored_but_excluded_from_prices(conn, tmp_path):
    p = img(tmp_path, "a.png", "white")
    out = add(conn, tmp_path, p, FakeExtractor({sha256_file(p): BAD}))
    assert out.status == "needs_review"
    assert db.get_receipt(conn, out.receipt_id)["status"] == "needs_review"
    assert db.observations(conn, "MILK") == []
    db.set_status(conn, out.receipt_id, "verified", "checked by hand")
    assert len(db.observations(conn, "MILK")) == 1


def test_duplicate_image_rejected_before_any_api_call(conn, tmp_path):
    p = img(tmp_path, "a.png", "white")
    fx = FakeExtractor({sha256_file(p): GOOD})
    add(conn, tmp_path, p, fx)
    with pytest.raises(DuplicateReceiptError):
        add(conn, tmp_path, p, fx)
    assert fx.calls == 1
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1


def test_save_is_all_or_nothing(conn, tmp_path):
    p = img(tmp_path, "a.png", "white")
    broken = receipt([line("MILK 3.49", "3.49"), line("BAD", "1.00")], subtotal="4.49", total="4.49")
    fx = FakeExtractor({sha256_file(p): broken})
    # Force the second line insert to fail inside the transaction.
    conn.execute("""CREATE TRIGGER fail_second BEFORE INSERT ON line_items WHEN NEW.line_no = 2
                    BEGIN SELECT RAISE(ABORT, 'boom'); END""")
    with pytest.raises(sqlite3.DatabaseError):
        add(conn, tmp_path, p, fx)
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM line_items").fetchone()[0] == 0


def test_extractor_error_stores_nothing(conn, tmp_path):
    p = img(tmp_path, "a.png", "white")
    with pytest.raises(MalformedOutputError):
        add(conn, tmp_path, p, FakeExtractor({sha256_file(p): MalformedOutputError("bad json")}))
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 0


def test_cancel_sends_nothing(conn, tmp_path):
    p = img(tmp_path, "a.png", "white")
    fx = FakeExtractor({sha256_file(p): GOOD})
    with pytest.raises(Cancelled):
        add(conn, tmp_path, p, fx, confirm=lambda prepared, preview: False)
    assert fx.calls == 0


def test_correction_reverifies(conn, tmp_path):
    p = img(tmp_path, "a.png", "white")
    out = add(conn, tmp_path, p, FakeExtractor({sha256_file(p): BAD}))
    fixed = BAD.model_copy(update={"subtotal": "3.49", "total": "3.49"})
    from pricelens.verify import verify
    db.replace_extraction(conn, out.receipt_id, verify(fixed, TODAY), fixed.model_dump_json())
    assert db.get_receipt(conn, out.receipt_id)["status"] == "verified"
    assert db.get_receipt(conn, out.receipt_id)["reviewed_at"] is not None


def test_price_history_across_receipts(conn, tmp_path):
    a, b = img(tmp_path, "a.png", "white"), img(tmp_path, "b.png", "gray")
    r2 = receipt([line("MILK 3.99", "3.99", desc="MILK")], subtotal="3.99", total="3.99",
                 date_raw="11/19/2026", date_iso="2026-11-19")
    fx = FakeExtractor({sha256_file(a): GOOD, sha256_file(b): r2})
    add(conn, tmp_path, a, fx)
    add(conn, tmp_path, b, fx)
    from pricelens.prices import price_changes
    [c] = price_changes(db.observations(conn, "milk"))
    assert (c.from_cents, c.to_cents) == (349, 399)


def test_store_names_are_matched_case_insensitively(conn, tmp_path):
    a, b = img(tmp_path, "a.png", "white"), img(tmp_path, "b.png", "gray")
    fx = FakeExtractor({sha256_file(a): GOOD, sha256_file(b): GOOD.model_copy(update={"store_name": "Test  Mart"})})
    add(conn, tmp_path, a, fx)
    add(conn, tmp_path, b, fx)
    assert conn.execute("SELECT COUNT(*) FROM stores").fetchone()[0] == 1


def test_replay_round_trip(conn, tmp_path):
    p = img(tmp_path, "a.png", "white")
    sha = sha256_file(p)
    fixtures = tmp_path / "fx"
    out = add(conn, tmp_path, p, FakeExtractor({sha: GOOD}), fixture_dir=fixtures)
    assert (fixtures / f"{sha}.json").exists()
    conn2 = db.connect(":memory:")
    out2 = add(conn2, tmp_path, p, ReplayExtractor(fixtures))
    assert out2.status == out.status == "verified"


def test_schema_validation_rejects_bad_output():
    with pytest.raises(MalformedOutputError):
        parse_output('{"store_name": "X"}')
    with pytest.raises(MalformedOutputError):
        parse_output("not json")


def test_strict_schema_shape():
    s = strict_json_schema(ExtractedReceipt)
    assert "$defs" not in json.dumps(s) and "$ref" not in json.dumps(s)
    assert s["additionalProperties"] is False
    assert set(s["required"]) == set(s["properties"])
    item = s["properties"]["lines"]["items"]
    assert item["additionalProperties"] is False and set(item["required"]) == set(item["properties"])
