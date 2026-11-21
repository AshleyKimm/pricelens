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
from .prices import price_changes
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


def cmd_add(args) -> int:
    if args.replay:
        from .extract import ReplayExtractor
        extractor = ReplayExtractor(Path(args.replay))
    elif args.provider == "gemini":
        from .gemini import DEFAULT_GEMINI_MODEL, GeminiExtractor
        extractor = GeminiExtractor(model=args.model or DEFAULT_GEMINI_MODEL)
    else:
        from .extract import DEFAULT_MODEL, ClaudeExtractor
        extractor = ClaudeExtractor(model=args.model or DEFAULT_MODEL, effort=args.effort)
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
    print(f"{'date':<10}  {'store':<22} {'item':<28} {'unit':<5} {'price':>10}")
    for o in sorted(obs, key=lambda o: (o.store, o.description, o.day)):
        print(f"{o.day}  {o.store:<22} {o.description:<28} {o.unit:<5} {format_amount(o.unit_price_cents):>10}")
    changes = price_changes(obs)
    if changes:
        print("\nPrice changes:")
        for c in changes:
            arrow = "UP  " if c.change_cents > 0 else "DOWN"
            print(f"  {arrow} {c.store} / {c.description}: {format_amount(c.from_cents)} -> "
                  f"{format_amount(c.to_cents)} ({c.change_pct:+.1f}%) {c.from_day} -> {c.to_day}")
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

    r = sub.add_parser("review", help="list, inspect, approve, reject or correct receipts")
    r.add_argument("action", choices=["list", "show", "approve", "reject", "export", "import"])
    r.add_argument("id", nargs="?", type=int)
    r.add_argument("file", nargs="?")
    r.add_argument("--note")
    r.add_argument("--all", action="store_true", help="list: include verified/rejected")
    r.set_defaults(func=cmd_review)

    pr = sub.add_parser("prices", help="price history and changes for an item")
    pr.add_argument("query")
    pr.set_defaults(func=cmd_prices)
    return p


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
