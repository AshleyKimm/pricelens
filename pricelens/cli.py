"""Command line interface: python -m pricelens <command> ..."""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date
from pathlib import Path

from . import db
from .errors import PriceLensError
from .money import format_amount
from .pipeline import Cancelled, add_receipt
from .prices import latest_by_store, price_changes
from .verify import verify

ROOT = Path.cwd()


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _conn():
    return db.connect(_env("PRICELENS_DB", str(ROOT / "data" / "pricelens.db")))


def _print_issues(issues) -> None:
    for i in issues:
        tag = "FAIL" if i["blocking"] else "note"
        print(f"  [{tag}] {i['code']}: {i['message']}")


def _confirm_interactive(prepared, preview: Path) -> bool:
    print(f"Redacted preview (exactly what will be sent): {preview}")
    print(f"  OCR read {prepared.ocr_word_count} words; blacked out {len(prepared.boxes)} region(s).")
    if not prepared.boxes:
        print("  WARNING: nothing was redacted. Most receipts have a card or loyalty line - check the preview.")
    if hasattr(os, "startfile"):
        try:
            os.startfile(preview)  # opens the preview in the default image viewer (Windows)
        except OSError:
            pass
    return input("Send this image to the API? [y/N] ").strip().lower() == "y"


def _extractor(args):
    if getattr(args, "replay", None):
        from .extract import ReplayExtractor
        return ReplayExtractor(Path(args.replay))
    if args.provider == "gemini":
        from .gemini import DEFAULT_GEMINI_MODEL, GeminiExtractor
        return GeminiExtractor(model=args.model or DEFAULT_GEMINI_MODEL)
    from .extract import DEFAULT_MODEL, ClaudeExtractor
    return ClaudeExtractor(model=args.model or DEFAULT_MODEL, effort=args.effort)


def _caller(args):
    """One shared Caller per command: retries, rate limit, budget, circuit breaker (Layer 4)."""
    from .reliability import Budget, Caller, CircuitBreaker, RateLimiter, RetryPolicy
    if getattr(args, "replay", None):  # saved responses: no network, so no budget or rate limit
        return Caller(policy=RetryPolicy(), limiter=RateLimiter(None), budget=Budget(None), breaker=CircuitBreaker())
    return Caller(policy=RetryPolicy(), limiter=RateLimiter(args.rpm), budget=Budget(args.budget),
                  breaker=CircuitBreaker())


def cmd_add(args) -> int:
    extractor = _extractor(args)
    conn = _conn()
    try:
        out = add_receipt(
            Path(args.image), conn=conn, extractor=extractor,
            redacted_dir=ROOT / "data" / "redacted",
            confirm=None if args.yes else _confirm_interactive,
            fixture_dir=Path(args.save_fixture) if args.save_fixture else None,
            locale=args.locale,
        )
    except Cancelled:
        print("Cancelled; nothing was sent.")
        return 1
    rec = out.result.receipt
    print(f"Receipt #{out.receipt_id}: {out.status.upper()}  ({out.model})")
    print(f"  {rec.store_name} | {rec.purchase_date} | total {format_amount(rec.total_cents, rec.currency)} "
          f"| {len(rec.lines)} lines")
    _print_issues([i.to_dict() for i in out.issues])
    if out.status != "verified":
        print("  Not used in price history until reviewed: python -m pricelens review show", out.receipt_id)
    return 0


def cmd_review(args) -> int:
    conn = _conn()
    if args.action == "list":
        rows = db.list_receipts(conn, None if args.all else "needs_review")
        for r in rows:
            reasons = [x["code"] for x in json.loads(r["review_reasons"]) if x["blocking"]]
            print(f"#{r['id']:<4} {r['status']:<13} {r['purchase_date'] or '?':<10} {r['store'] or '?':<24} "
                  f"{','.join(reasons)}")
        if not rows:
            print("Nothing to review.")
        return 0

    r = db.get_receipt(conn, args.id)
    if r is None:
        print(f"No receipt #{args.id}", file=sys.stderr)
        return 1

    if args.action == "show":
        cur = r["currency"]
        print(f"#{r['id']} {r['status']}  store={r['store_name_raw']!r} date={r['purchase_date']} "
              f"(raw {r['date_raw']!r}) model={r['model']} prompt={r['prompt_version']}")
        print(f"subtotal {format_amount(r['subtotal_cents'], cur)}  tax {format_amount(r['tax_cents'], cur)}  "
              f"total {format_amount(r['total_cents'], cur)}  legibility={r['legibility']}")
        print(f"image sent: {r['redacted_path']}")
        for l in db.get_lines(conn, args.id):
            print(f"  {l.line_no:>2} {l.kind:<8} {l.section:<14} {format_amount(l.line_total_cents, cur):>12}  "
                  f"{l.raw_text.replace(chr(10), ' / ')}")
        _print_issues(json.loads(r["review_reasons"]))
    elif args.action == "approve":
        db.set_status(conn, args.id, "verified", args.note or "approved by reviewer")
        print(f"#{args.id} approved")
    elif args.action == "reject":
        db.set_status(conn, args.id, "rejected", args.note or "rejected by reviewer")
        print(f"#{args.id} rejected")
    elif args.action == "export":
        Path(args.file).write_text(json.dumps(json.loads(r["extraction_json"]), indent=2), encoding="utf-8")
        print(f"Wrote {args.file}. Edit it, then: python -m pricelens review import {args.id} {args.file}")
    elif args.action == "import":
        from .extract import parse_output
        text = Path(args.file).read_text(encoding="utf-8")
        result = verify(parse_output(text), locale=_env("PRICELENS_LOCALE", "US"))
        db.replace_extraction(conn, args.id, result, text)
        print(f"#{args.id} re-verified: {result.status.upper()}")
        _print_issues([i.to_dict() for i in result.issues])
    return 0


def cmd_prices(args) -> int:
    conn = _conn()
    obs = db.observations(conn, args.query)
    if not obs:
        print("No verified purchases match.")
        return 0
    from .prices import comparable_by_unit_price
    from .units import format_unit_price
    up = lambda o: format_unit_price(o.base_price, o.base_unit, args.units)
    print(f"{'date':<10}  {'store':<18} {'product':<14} {'variety':<10} {'printed as':<18} {'pack':<9} "
          f"{'price':>8}  {'unit price':<14}")
    for o in sorted(obs, key=lambda o: (o.description, o.variety, o.store, o.day)):
        print(f"{o.day}  {o.store:<18} {o.description:<14} {o.variety or '-':<10} {o.printed[:18]:<18} "
              f"{o.pack or '-':<9} {format_amount(o.unit_price_cents):>8}  {up(o) or '-':<14}")
    changes = price_changes(obs)
    if changes:
        print("\nPrice changes:")
        for c in changes:
            arrow = "UP  " if c.change_cents > 0 else "DOWN"
            name = c.description + "".join(f" ({x})" for x in (c.variety, c.pack) if x)
            print(f"  {arrow} {c.store} / {name}: {format_amount(c.from_cents)} -> "
                  f"{format_amount(c.to_cents)} ({c.change_pct:+.1f}%) {c.from_day} -> {c.to_day}")
    compare = {k: v for k, v in latest_by_store(obs).items() if len(v) > 1}
    if compare:
        print("\nLatest price by store (cheapest first):")
        for (product, unit), rows in sorted(compare.items()):
            by_unit = comparable_by_unit_price(rows)
            print(f"  {product} ({'by unit price' if by_unit else 'per package: pack sizes unknown'}): " + ", ".join(
                f"{o.store}{''.join(' [' + x + ']' for x in (o.variety, o.pack) if x)} "
                f"{up(o) if by_unit else format_amount(o.unit_price_cents)} ({o.day})" for o in rows))
    return 0


def _categorizer(args):
    if args.provider == "gemini":
        from .categorize import GeminiCategorizer
        from .gemini import DEFAULT_GEMINI_CATEGORIZE_MODEL
        return GeminiCategorizer(model=args.model or DEFAULT_GEMINI_CATEGORIZE_MODEL)
    from .categorize import ClaudeCategorizer
    from .extract import DEFAULT_MODEL
    return ClaudeCategorizer(model=args.model or DEFAULT_MODEL)


def cmd_categorize(args) -> int:
    from .categorize import PROMPT_VERSION
    from .labeling import categorize_all, drop_outdated_ai_labels
    conn = _conn()
    if args.relabel:
        n = drop_outdated_ai_labels(conn, PROMPT_VERSION)
        print(f"Dropped {n} AI labels from older prompts (human corrections kept); re-labeling with {PROMPT_VERSION}.")
    from .reliability import StopRun
    caller = _caller(args)
    try:
        results = categorize_all(conn, _categorizer(args), [args.receipt] if args.receipt else None,
                                 call=caller.call)
    except StopRun as e:
        print(f"Stopped: {type(e).__name__}: {e}. Labels saved so far are kept; run categorize again later.")
        return 1
    for s in results:
        who = f"{s.store or '?'} (receipts {','.join(map(str, s.receipt_ids))})"
        if s.error:
            print(f"{who}: ERROR {s.error}")
        elif s.skipped:
            print(f"{who}: skipped ({s.skipped})")
        else:
            print(f"{who}: {s.calls} call(s), {s.new_labels} new, {s.cached} cached, {s.flagged} flagged")
            for f in s.flagged_items:
                print(f"    review: {f}")
    n = sum(len(s.receipt_ids) for s in results)
    print(f"\n{n} receipts, {sum(s.calls for s in results)} API calls, "
          f"{sum(s.new_labels for s in results)} new labels, {sum(s.cached for s in results)} cache hits, "
          f"{sum(s.flagged for s in results)} flagged, {sum(1 for s in results if s.error)} errors")
    return 1 if any(s.error for s in results) else 0


def cmd_label(args) -> int:
    from .labeling import fix_label, list_labels
    conn = _conn()
    if args.action == "list":
        rows = list_labels(conn, needs_review_only=args.review)
        for r in rows:
            flag = "REVIEW" if r["needs_review"] else ""
            reasons = "; ".join(json.loads(r["review_reasons"]))
            print(f"{r['store'][:16]:<16} {r['description'][:20]:<20} -> {r['normalized_name'] or '?':<16} "
                  f"{r['variety'] or '-':<12} "
                  f"{r['category']:<15} {r['confidence'] or '':<6} {r['source']:<5} {flag} {reasons}")
        print(f"{len(rows)} labels")
        return 0
    if None in (args.store, args.description, args.name, args.category):
        print("label fix STORE DESCRIPTION NAME CATEGORY", file=sys.stderr)
        return 2
    try:
        path = fix_label(conn, args.store, args.description, args.name, args.category,
                         variety=args.variety or "")
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    print(f"Saved. Recorded as a regression case in {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pricelens")
    sub = p.add_subparsers(dest="command", required=True)

    a = sub.add_parser("add", help="extract, verify and store one receipt image")
    a.add_argument("image")
    a.add_argument("--yes", action="store_true", help="skip the redacted-preview confirmation")
    a.add_argument("--replay", metavar="DIR", help="use saved extractions instead of calling the API")
    a.add_argument("--save-fixture", metavar="DIR", help="save the raw model output for replay")
    a.add_argument("--provider", default=_env("PRICELENS_PROVIDER", "gemini"), choices=["gemini", "claude"])
    a.add_argument("--model", default=os.environ.get("PRICELENS_MODEL"), help="defaults per provider")
    a.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"],
                   help="Claude only")
    a.add_argument("--locale", default=_env("PRICELENS_LOCALE", "US"))
    a.set_defaults(func=cmd_add)

    c = sub.add_parser("categorize", help="assign categories and normalized names (cached)")
    c.add_argument("--receipt", type=int, help="only this receipt (default: all)")
    c.add_argument("--provider", default=_env("PRICELENS_PROVIDER", "gemini"), choices=["gemini", "claude"])
    c.add_argument("--model", default=os.environ.get("PRICELENS_CATEGORIZE_MODEL"))
    c.add_argument("--relabel", action="store_true",
                   help="re-label AI labels made by an older prompt version (human corrections are kept)")
    _reliability_args(c)
    c.set_defaults(func=cmd_categorize)

    b = sub.add_parser("batch", help="add every receipt image in a folder (parallel, resumable)")
    b.add_argument("folder")
    b.add_argument("--workers", type=int, default=2)
    _reliability_args(b)
    _extractor_args(b)
    b.set_defaults(func=cmd_batch)

    rf = sub.add_parser("retry-failed", help="re-run only the receipts that failed with a retryable error")
    rf.add_argument("--workers", type=int, default=2)
    _reliability_args(rf)
    _extractor_args(rf)
    rf.set_defaults(func=cmd_batch, folder=None)

    v = sub.add_parser("verify", help="re-check stored records and report problems (fixes nothing)")
    v.set_defaults(func=cmd_verify)

    lb = sub.add_parser("label", help="list labels or correct one by hand")
    lb.add_argument("action", choices=["list", "fix"])
    lb.add_argument("store", nargs="?")
    lb.add_argument("description", nargs="?", help="printed item name, e.g. 'TP MINT'")
    lb.add_argument("name", nargs="?", help="correct normalized name, e.g. toothpaste")
    lb.add_argument("category", nargs="?")
    lb.add_argument("--review", action="store_true", help="list: only labels needing review")
    lb.add_argument("--variety", help="fix: e.g. jasmine, whole, organic")
    lb.set_defaults(func=cmd_label)

    r = sub.add_parser("review", help="list, inspect, approve, reject or correct receipts")
    r.add_argument("action", choices=["list", "show", "approve", "reject", "export", "import"])
    r.add_argument("id", nargs="?", type=int)
    r.add_argument("file", nargs="?")
    r.add_argument("--note")
    r.add_argument("--all", action="store_true", help="list: include verified/rejected")
    r.set_defaults(func=cmd_review)

    pr = sub.add_parser("prices", help="price history and changes for an item")
    pr.add_argument("query")
    pr.add_argument("--units", default=_env("PRICELENS_UNITS", "imperial"), choices=["imperial", "metric"])
    pr.set_defaults(func=cmd_prices)

    sv = sub.add_parser("serve", help="run the web interface")
    sv.add_argument("--mode", default=_env("PRICELENS_MODE", "local"), choices=["local", "demo"])
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    sv.set_defaults(func=cmd_serve)

    dm = sub.add_parser("demo", help="export or rebuild the pre-computed public demo (no API calls)")
    dm.add_argument("action", choices=["export", "build"])
    dm.set_defaults(func=cmd_demo)
    return p


def _reliability_args(sp):
    sp.add_argument("--rpm", type=float, default=float(_env("PRICELENS_RPM", "10")),
                    help="max requests per minute (default 10)")
    sp.add_argument("--budget", type=int, default=int(_env("PRICELENS_BUDGET", "15")),
                    help="max requests this run may send, retries included (default 15; free tier: 20/day)")


def _extractor_args(sp):
    sp.add_argument("--provider", default=_env("PRICELENS_PROVIDER", "gemini"), choices=["gemini", "claude"])
    sp.add_argument("--model", default=os.environ.get("PRICELENS_MODEL"))
    sp.add_argument("--effort", default="medium", choices=["low", "medium", "high", "xhigh", "max"])
    sp.add_argument("--replay", metavar="DIR", help="use saved extractions instead of calling the API")
    sp.add_argument("--save-fixture", metavar="DIR")
    sp.add_argument("--locale", default=_env("PRICELENS_LOCALE", "US"))


def cmd_batch(args) -> int:
    from .batch import failed_paths, find_images, run_batch
    conn = _conn()
    if args.folder:
        paths, source = find_images(Path(args.folder)), str(args.folder)
    else:
        paths, source = failed_paths(conn), "retry-failed"
    if not paths:
        print("Nothing to do." if args.folder is None else f"No images in {args.folder}.")
        return 0
    print(f"{len(paths)} image(s), {args.workers} worker(s), <= {args.rpm:g} requests/min, budget {args.budget}. "
          f"No preview prompts in batch mode: images are redacted locally as usual.")
    s = run_batch(conn, paths, extractor=_extractor(args), caller=_caller(args), redacted_dir=ROOT / "data" / "redacted",
                  workers=args.workers, source=source, locale=args.locale,
                  fixture_dir=Path(args.save_fixture) if args.save_fixture else None, on_progress=print)
    print(f"\nRun #{s.run_id}: {s.line()}")
    if s.stopped:
        print(f"Stopped early: {s.stopped}")
    if (n := len(failed_paths(conn))):
        print(f"{n} can be retried later with: python -m pricelens retry-failed")
    return 1 if s.failed or s.stopped else 0


def cmd_verify(args) -> int:
    from .audit import audit
    findings = audit(_conn(), ROOT / "data" / "redacted")
    for f in findings:
        print(f"[{f.code}] {'#' + str(f.receipt_id) if f.receipt_id else '-':>5}  {f.message}")
    print(f"{len(findings)} finding(s)" if findings else "No problems found.")
    return 1 if findings else 0


def cmd_serve(args) -> int:
    import uvicorn
    from .web.app import create_app
    if args.mode == "local" and args.host not in ("127.0.0.1", "localhost", "::1"):
        print("Local mode can write to your database, so it only listens on this computer (127.0.0.1).",
              file=sys.stderr)
        return 2
    # Demo always uses the rebuilt data/demo.db; local mode honours PRICELENS_DB like every other command.
    db_path = os.environ.get("PRICELENS_DB") if args.mode == "local" else None
    app = create_app(mode=args.mode, root=ROOT, db_path=db_path)
    print(f"PriceLens ({args.mode} mode) on http://{args.host}:{args.port}  (Ctrl+C to stop)")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_demo(args) -> int:
    from . import demo
    if args.action == "export":
        src = os.environ.get("PRICELENS_DB", str(ROOT / "data" / "demo.db"))
        out = demo.export(db.connect(src), ROOT)
        print(f"Exported {out['images']} redacted images and {out['labels']} labels from {src} to demo/")
    else:
        out = demo.build(ROOT, ROOT / "data" / "demo.db")
        print(f"Built data/demo.db: {out['receipts']} receipts, {out['labels']} labels (0 API calls)")
    return 0


def main(argv=None) -> int:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    args = build_parser().parse_args(argv)
    if args.command == "review" and args.action != "list" and args.id is None:
        print("review: an id is required", file=sys.stderr)
        return 2
    if args.command == "review" and args.action in ("export", "import") and not args.file:
        print("review: a file path is required", file=sys.stderr)
        return 2
    try:
        return args.func(args)
    except PriceLensError as e:
        print(f"error ({type(e).__name__}): {e}", file=sys.stderr)
        return 1
