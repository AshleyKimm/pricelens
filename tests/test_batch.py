"""Batch ingest: partial failures, idempotent re-runs, retry-failed, stopping cleanly, concurrency, audit."""
import threading
from datetime import date
from pathlib import Path

import pytest
from helpers import line, receipt
from PIL import Image

from pricelens import db
from pricelens.audit import audit
from pricelens.batch import failed_paths, find_images, run_batch
from pricelens.errors import AuthError, MalformedOutputError, RateLimitError, TransientAPIError
from pricelens.extract import FakeExtractor
from pricelens.redact import prepare, sha256_file
from pricelens.reliability import Budget, Caller, CircuitBreaker, RetryPolicy

TODAY = date(2026, 11, 21)
no_ocr = lambda p: prepare(p, ocr=lambda im: [])


def rec(i):
    amt = f"{1 + i // 100}.{i % 100:02d}"
    return receipt([line(f"ITEM{i} {amt}", amt, desc=f"ITEM{i}")], subtotal=amt, total=amt, store=f"STORE {i}")


@pytest.fixture
def folder(tmp_path):
    d = tmp_path / "in"
    d.mkdir()
    for i in range(6):
        Image.new("RGB", (60, 80), (i * 40, 100, 100)).save(d / f"r{i}.png")
    (d / "notes.txt").write_text("not an image")
    return d


def fast_caller(**kw):
    return Caller(policy=RetryPolicy(max_attempts=3, base=0.001, cap=0.001),
                  breaker=kw.pop("breaker", CircuitBreaker(threshold=99)), sleep=lambda s: None, **kw)


def go(conn, paths, fx, tmp_path, caller=None, workers=3):
    return run_batch(conn, paths, extractor=fx, caller=caller or fast_caller(), redacted_dir=tmp_path / "red",
                     workers=workers, source="test", prepare_fn=no_ocr, today=TODAY)


def answers(folder, overrides=None):
    out = {sha256_file(p): rec(i) for i, p in enumerate(find_images(folder))}
    for name, v in (overrides or {}).items():
        out[sha256_file(folder / name)] = v
    return out


@pytest.fixture
def conn():
    return db.connect(":memory:")


def test_finds_only_images(folder):
    assert [p.name for p in find_images(folder)] == [f"r{i}.png" for i in range(6)]


def test_failures_dont_stop_the_batch_and_are_recorded(conn, folder, tmp_path):
    fx = FakeExtractor(answers(folder, {"r1.png": MalformedOutputError("bad json"),
                                        "r3.png": TransientAPIError("503")}))
    s = go(conn, find_images(folder), fx, tmp_path)
    assert (s.verified, s.failed) == (4, 2)
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 4
    rows = {Path(r[0]).name: (r[1], r[2]) for r in conn.execute(
        "SELECT image_path, error_type, retryable FROM ingest_failures")}
    assert rows == {"r1.png": ("MalformedOutputError", 0), "r3.png": ("TransientAPIError", 1)}
    assert "4 verified" in s.line() and "2 failed" in s.line()


def test_rerunning_a_batch_inserts_nothing_twice(conn, folder, tmp_path):
    fx = FakeExtractor(answers(folder))
    go(conn, find_images(folder), fx, tmp_path)
    calls = fx.calls
    s = go(conn, find_images(folder), fx, tmp_path)
    assert (s.verified, s.already_stored) == (0, 6)
    assert fx.calls == calls                                # no API calls for stored images
    assert conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 6


def test_same_image_twice_in_one_folder(conn, folder, tmp_path):
    (folder / "copy.png").write_bytes((folder / "r0.png").read_bytes())
    s = go(conn, find_images(folder), FakeExtractor(answers(folder)), tmp_path)
    assert (s.verified, s.already_stored) == (6, 1)


def test_transient_error_is_retried_within_the_run(conn, folder, tmp_path):
    a = answers(folder)
    sha = sha256_file(folder / "r2.png")
    a[sha] = [TransientAPIError("503"), a[sha]]             # fails once, then works
    s = go(conn, find_images(folder), FakeExtractor(a), tmp_path)
    assert (s.verified, s.failed, s.retries) == (6, 0, 1)


def test_retry_failed_reprocesses_only_the_failures(conn, folder, tmp_path):
    a = answers(folder, {"r3.png": TransientAPIError("503")})
    go(conn, find_images(folder), FakeExtractor(a), tmp_path)
    assert [p.name for p in failed_paths(conn)] == ["r3.png"]
    fixed = FakeExtractor(answers(folder))
    s = go(conn, failed_paths(conn), fixed, tmp_path)
    assert (s.verified, fixed.calls) == (1, 1)               # only r3 was sent
    assert failed_paths(conn) == []                          # resolved


def test_permanent_failures_are_not_retried_later(conn, folder, tmp_path):
    go(conn, find_images(folder), FakeExtractor(answers(folder, {"r1.png": MalformedOutputError("x")})), tmp_path)
    assert failed_paths(conn) == []


def test_daily_quota_stops_the_run_and_the_rest_waits_for_retry_failed(conn, folder, tmp_path):
    daily = RateLimitError("limit: 20 requests per day on Free Tier. Please retry in 15h9m7s")
    a = {sha: daily for sha in answers(folder)}
    a[sha256_file(folder / "r0.png")] = rec(0)
    s = go(conn, find_images(folder), FakeExtractor(a), tmp_path, workers=1)
    assert s.stopped and "DailyQuotaExceeded" in s.stopped
    assert s.verified == 1 and s.failed == 1 and s.not_attempted == 4
    assert len(failed_paths(conn)) == 5                      # all resumable tomorrow
    run = conn.execute("SELECT stopped, finished_at FROM ingest_runs").fetchone()
    assert run[0] and run[1]


def test_budget_stops_the_run_cleanly(conn, folder, tmp_path):
    s = go(conn, find_images(folder), FakeExtractor(answers(folder)), tmp_path, caller=fast_caller(budget=Budget(3)),
           workers=1)
    assert s.verified == 3 and "BudgetExhausted" in s.stopped and s.budget_left == 0
    assert s.failed + s.not_attempted == 3


def test_auth_failure_stops_the_run(conn, folder, tmp_path):
    s = go(conn, find_images(folder), FakeExtractor({sha: AuthError("bad key") for sha in answers(folder)}),
           tmp_path, workers=1)
    assert "AuthError" in s.stopped and s.verified == 0
    assert conn.execute("SELECT retryable FROM ingest_failures WHERE error_type = 'AuthError'").fetchone()[0] == 0


def test_free_tier_policy_applies_to_batches(conn, folder, tmp_path):
    fx = FakeExtractor(answers(folder))
    fx.trains_on_input = True                                # folder is not a public-data folder
    s = go(conn, find_images(folder), fx, tmp_path)
    assert s.failed == 6 and fx.calls == 0
    assert failed_paths(conn) == []                          # not retryable: needs a different provider


def test_eight_workers_forty_receipts_no_races(tmp_path):
    """Many workers, one writer: every receipt stored exactly once, all writes from the main thread."""
    d = tmp_path / "many"
    d.mkdir()
    for i in range(40):
        Image.new("RGB", (40, 40), (i, i * 3 % 255, 7)).save(d / f"r{i:02d}.png")
    a = {sha256_file(p): rec(i) for i, p in enumerate(find_images(d))}
    a[sha256_file(d / "r05.png")] = [TransientAPIError("503"), a[sha256_file(d / "r05.png")]]
    conn = db.connect(tmp_path / "many.db")                  # file DB: check_same_thread enforces one writer thread
    writer_threads = set()
    orig = conn.execute

    class Spy:
        def __getattr__(self, n):
            return getattr(conn, n)

        def execute(self, *a, **k):
            writer_threads.add(threading.current_thread().name)
            return orig(*a, **k)

        def __enter__(self):
            return conn.__enter__()

        def __exit__(self, *e):
            return conn.__exit__(*e)
    s = run_batch(Spy(), find_images(d), extractor=FakeExtractor(a), caller=fast_caller(), redacted_dir=tmp_path / "r",
                  workers=8, source="race", prepare_fn=no_ocr, today=TODAY)
    assert (s.verified, s.failed) == (40, 0)
    assert tuple(conn.execute("SELECT COUNT(*), COUNT(DISTINCT image_sha256) FROM receipts").fetchone()) == (40, 40)
    assert writer_threads == {threading.current_thread().name}


def test_likely_duplicate_photo_is_flagged_not_dropped(conn, folder, tmp_path):
    # Two different photos (different bytes) of the same receipt: same store, date and total.
    a = answers(folder)
    a[sha256_file(folder / "r1.png")] = rec(0)
    s = go(conn, [folder / "r0.png", folder / "r1.png"], FakeExtractor(a), tmp_path, workers=1)
    assert (s.verified, s.flagged) == (1, 1)
    reasons = conn.execute("SELECT review_reasons FROM receipts WHERE status = 'needs_review'").fetchone()[0]
    assert "D1" in reasons and "#1" in reasons


# --- pricelens verify ---------------------------------------------------------------------------

def test_audit_clean_database_has_no_findings(conn, folder, tmp_path):
    go(conn, find_images(folder), FakeExtractor(answers(folder)), tmp_path)
    assert audit(conn, tmp_path / "red") == []


def test_audit_finds_problems(conn, folder, tmp_path):
    go(conn, find_images(folder), FakeExtractor(answers(folder, {"r5.png": TransientAPIError("503")})), tmp_path)
    # Workers finish in any order, so receipt ids vary between runs: look receipts up by store, not by id.
    rid = lambda i: conn.execute("SELECT id FROM receipts WHERE store_name_raw = ?", (f"STORE {i}",)).fetchone()[0]
    conn.execute("UPDATE line_items SET line_total_cents = 999 WHERE receipt_id = ?", (rid(0),))  # tampered line
    conn.execute("UPDATE receipts SET extraction_json = REPLACE(extraction_json, '\"total\":\"1.01\"', "
                 "'\"total\":\"9.99\"') WHERE id = ?", (rid(1),))                               # rules now fail
    (tmp_path / "red" / (conn.execute("SELECT image_sha256 FROM receipts WHERE id = ?", (rid(2),)).fetchone()[0]
                         + ".jpg")).unlink()
    codes = {(f.code, f.receipt_id) for f in audit(conn, tmp_path / "red")}
    assert {("A2", rid(0)), ("A1", rid(1)), ("A3", rid(2)), ("A5", None)} <= codes


def test_audit_respects_human_decisions(conn, folder, tmp_path):
    go(conn, [folder / "r0.png"], FakeExtractor(answers(folder)), tmp_path)
    conn.execute("UPDATE receipts SET extraction_json = REPLACE(extraction_json, '\"total\":\"1.00\"', '\"total\":\"9.99\"')")
    db.set_status(conn, 1, "verified", "checked the paper receipt")
    assert not any(f.code == "A1" for f in audit(conn))
