"""Batch ingest: a folder of receipts, processed in parallel, with partial-failure handling.

Concurrency model:
- Worker threads (ThreadPoolExecutor) run pipeline.process(): OCR redaction + the API call + verification.
  That work is mostly waiting on the network, so threads are the right tool (no multiprocessing needed).
- Every API request goes through ONE shared reliability.Caller: rate limit, budget, circuit breaker, retries.
- Only the main thread touches SQLite (the connection would refuse other threads anyway), so there are no
  "database is locked" races; each receipt is still its own transaction.
- One failed receipt never stops the batch. A StopRun (daily quota, budget, circuit open) or an auth
  failure stops sending new work; everything not attempted is recorded as retryable for `retry-failed`.
"""
from __future__ import annotations

import json
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Callable

from . import db
from .errors import AuthError, DuplicateReceiptError, PriceLensError
from .extract import Extractor
from .pipeline import process, store
from .policy import DataPolicyError, check_allowed
from .redact import PreparedImage, prepare, sha256_file
from .reliability import Caller, StopRun, is_retryable

IMAGE_TYPES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".bmp", ".tif", ".tiff"}


@dataclass
class BatchSummary:
    run_id: int
    verified: int = 0
    flagged: int = 0
    failed: int = 0
    already_stored: int = 0
    not_attempted: int = 0
    requests: int = 0
    retries: int = 0
    budget_left: int | None = None
    stopped: str | None = None
    failures: list[tuple[str, str, str]] = field(default_factory=list)  # (file, error type, message)

    def line(self) -> str:
        parts = [f"{self.verified} verified", f"{self.flagged} flagged", f"{self.failed} failed",
                 f"{self.already_stored} already stored"]
        if self.not_attempted:
            parts.append(f"{self.not_attempted} not attempted")
        parts.append(f"{self.requests} requests ({self.retries} retries)")
        if self.budget_left is not None:
            parts.append(f"{self.budget_left} left in budget")
        return " | ".join(parts)  # plain ASCII: Windows consoles mangle "·"


def find_images(folder: Path) -> list[Path]:
    return sorted(p for p in Path(folder).rglob("*") if p.is_file() and p.suffix.lower() in IMAGE_TYPES)


def _record_failure(conn, run_id, path, sha, err_type, message, attempts, retryable):
    with conn:
        conn.execute("""INSERT INTO ingest_failures(run_id, image_path, sha256, error_type, message, attempts, retryable)
                        VALUES (?, ?, ?, ?, ?, ?, ?)""",
                     (run_id, str(path), sha, err_type, message[:500], attempts, int(retryable)))


def _resolve(conn, sha: str) -> None:
    with conn:
        conn.execute("UPDATE ingest_failures SET resolved_at = datetime('now') WHERE sha256 = ? AND resolved_at IS NULL",
                     (sha,))


def run_batch(
    conn,
    paths: list[Path],
    *,
    extractor: Extractor,
    caller: Caller,
    redacted_dir: Path,
    workers: int = 2,
    source: str = "",
    prepare_fn: Callable[[Path], PreparedImage] = prepare,
    fixture_dir: Path | None = None,
    today: date | None = None,
    locale: str = "US",
    on_progress: Callable[[str], None] = lambda s: None,
    allow_uncertain_redaction: bool = False,
) -> BatchSummary:
    with conn:
        run_id = conn.execute("INSERT INTO ingest_runs(source, workers, rpm, budget) VALUES (?, ?, ?, ?)",
                              (source, workers, 60 / caller.limiter.interval if caller.limiter.interval else None,
                               caller.budget.limit)).lastrowid
    s = BatchSummary(run_id)

    def fail(path, sha, err: Exception | None, attempts=0, err_type=None, retryable=None, message=None):
        etype = err_type or type(err).__name__
        msg = message or str(err)
        retry = is_retryable(err) or isinstance(err, StopRun) if retryable is None else retryable
        _record_failure(conn, run_id, path, sha, etype, msg, attempts, retry)
        s.failures.append((Path(path).name, etype, msg[:160]))
        on_progress(f"FAILED {Path(path).name}: {etype}: {msg[:120]}")

    # Pre-pass in the main thread: cheap checks before any OCR or API call.
    todo: list[tuple[Path, str]] = []
    for path in paths:
        try:
            if not path.is_file():
                raise FileNotFoundError(f"no such file: {path}")
            check_allowed(extractor, path)
            sha = sha256_file(path)
        except DataPolicyError as e:
            s.failed += 1
            fail(path, None, e, retryable=False)
            continue
        except OSError as e:
            s.failed += 1
            fail(path, None, e, err_type="UnreadableImageError", retryable=False)
            continue
        if db.find_by_sha(conn, sha) is not None:  # re-running a batch inserts nothing twice
            s.already_stored += 1
            _resolve(conn, sha)
            continue
        if sha in {t[1] for t in todo}:            # same image twice in one folder
            s.already_stored += 1
            continue
        todo.append((path, sha))

    stop: str | None = None
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="pricelens") as pool:
        pending = {}
        queue = list(todo)

        def submit_next():
            path, sha = queue.pop(0)
            fut = pool.submit(process, path, sha=sha, extractor=extractor, redacted_dir=redacted_dir,
                              prepare_fn=prepare_fn, fixture_dir=fixture_dir, today=today, locale=locale,
                              call=caller.call, allow_uncertain_redaction=allow_uncertain_redaction)
            pending[fut] = (path, sha)

        # Keep at most `workers` jobs in flight, so a stop leaves the rest cleanly "not attempted".
        while queue and len(pending) < workers:
            submit_next()
        while pending:
            done, _ = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                path, sha = pending.pop(fut)
                try:
                    out = store(conn, fut.result())  # main thread: the only DB writer
                except DuplicateReceiptError:
                    s.already_stored += 1
                    _resolve(conn, sha)
                except (StopRun, AuthError) as e:
                    s.failed += 1
                    fail(path, sha, e, retryable=not isinstance(e, AuthError))
                    stop = stop or f"{type(e).__name__}: {e}"
                except PriceLensError as e:
                    s.failed += 1
                    fail(path, sha, e)
                except Exception as e:  # unexpected bug in one receipt: record it, keep going
                    s.failed += 1
                    fail(path, sha, e, err_type=type(e).__name__, retryable=False)
                else:
                    _resolve(conn, sha)
                    if out.status == "verified":
                        s.verified += 1
                    else:
                        s.flagged += 1
                    on_progress(f"{out.status.upper():<12} {Path(path).name} -> receipt #{out.receipt_id}")
                if queue and not stop:
                    submit_next()

    for path, sha in queue:  # stopped early: everything left is recorded, so retry-failed can resume
        s.not_attempted += 1
        _record_failure(conn, run_id, path, sha, "NotAttempted", f"run stopped: {stop}", 0, True)

    s.requests, s.retries, s.budget_left, s.stopped = caller.requests, caller.retries, caller.budget.left, stop
    with conn:
        conn.execute("""UPDATE ingest_runs SET finished_at = datetime('now'), requests_sent = ?, stopped = ?, summary = ?
                        WHERE id = ?""",
                     (s.requests, stop, json.dumps({k: getattr(s, k) for k in (
                         "verified", "flagged", "failed", "already_stored", "not_attempted", "retries")}), run_id))
    return s


def failed_paths(conn) -> list[Path]:
    """Unresolved, retryable failures (latest record per image)."""
    rows = conn.execute("""SELECT image_path FROM ingest_failures
                           WHERE resolved_at IS NULL AND retryable = 1
                           GROUP BY image_path ORDER BY MIN(id)""").fetchall()
    return [Path(r[0]) for r in rows]
