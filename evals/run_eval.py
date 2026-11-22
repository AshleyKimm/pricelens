"""Eval runner. Scores model output against answer keys and writes evals/RESULTS.md.

    python evals/run_eval.py synthetic          # 0 API calls: reuses saved live extractions + labels
    python evals/run_eval.py redaction          # 0 API calls: local OCR before/after redaction
    python evals/run_eval.py cord --budget 15   # CORD-v2 sample: one extraction call per unpredicted receipt
    python evals/run_eval.py report             # rebuild RESULTS.md from saved results

Every number is `correct / scored` with a 95% Wilson interval, and every failure is listed.
Predictions are saved (evals/predictions/), so scoring is reproducible and free to re-run.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evals"))
sys.path.insert(0, str(ROOT / "evals" / "synthetic"))

from pricelens.demo import DEMO_AS_OF  # noqa: E402
from pricelens.models import ExtractedReceipt  # noqa: E402
from pricelens.money import MoneyParseError, parse_amount  # noqa: E402
from pricelens.redact import sha256_file  # noqa: E402
from pricelens.verify import verify  # noqa: E402

RESULTS = ROOT / "evals" / "results"
PRED = ROOT / "evals" / "predictions"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    d = 1 + z * z / n
    c = p + z * z / (2 * n)
    r = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))
    return (max(0.0, (c - r) / d), min(1.0, (c + r) / d))


def fmt(k: int, n: int) -> str:
    if n == 0:
        return "n/a"
    lo, hi = wilson(k, n)
    return f"{k}/{n} = {100 * k / n:.0f}% (95% CI {100 * lo:.0f}-{100 * hi:.0f}%)"


class Tally:
    def __init__(self):
        self.k, self.n, self.fail = defaultdict(int), defaultdict(int), defaultdict(list)

    def add(self, field: str, ok: bool, where: str = "", detail: str = ""):
        self.n[field] += 1
        self.k[field] += bool(ok)
        if not ok:
            self.fail[field].append(f"{where}: {detail}" if where else detail)

    def rows(self):
        return [(f, self.k[f], self.n[f]) for f in self.n]


def cents(s, cur="USD"):
    try:
        return parse_amount(s, cur)
    except MoneyParseError:
        return "unparseable"


# --- synthetic ------------------------------------------------------------------------------------

def eval_synthetic() -> dict:
    from cord import similarity  # same name matcher as CORD
    from generate import CATALOG, STORES
    t = Tally()
    flag = {"tp": [], "fn": [], "fp": [], "tn": []}  # flagged-and-should-be, missed, false alarm, correct pass
    caught_errors = {"wrong_and_flagged": 0, "wrong": 0}
    labels_dir = ROOT / "evals" / "synthetic" / "labels"
    for lp in sorted(labels_dir.glob("*.json")):
        L = json.loads(lp.read_text(encoding="utf-8"))
        img = ROOT / "evals" / "synthetic" / L["image"]
        pred_path = ROOT / "demo" / "extractions" / f"{sha256_file(img)}.json"
        if not pred_path.exists():
            t.add("receipts extracted", False, L["id"], "no saved extraction (not run yet)")
            continue
        t.add("receipts extracted", True)
        saved = json.loads(pred_path.read_text(encoding="utf-8"))
        ext = ExtractedReceipt.model_validate(saved["output"])
        res = verify(ext, today=DEMO_AS_OF)
        r = res.receipt
        wrong = False

        def check(field, got, want):
            nonlocal wrong
            ok = got == want
            wrong |= not ok
            t.add(field, ok, L["id"], f"expected {want!r}, got {got!r}")

        check("store", (r.store_name or "").upper(), L["store"])
        check("date", r.purchase_date.isoformat() if r.purchase_date else None, L["date"])
        check("subtotal (printed)", r.subtotal_cents, L["printed_subtotal_cents"])
        check("tax", r.tax_cents, L["tax_cents"])
        check("total (printed)", r.total_cents, L["printed_total_cents"])

        # line items: match by name similarity (+ price tie-break), like CORD
        got_lines = [l for l in r.lines]
        used = set()
        for want in L["lines"]:
            wname = want["printed"].split(" / ")[0].upper()
            best, best_s = None, 0.0
            for j, gl in enumerate(got_lines):
                if j in used:
                    continue
                s = similarity(wname, (gl.description or gl.raw_text.split("\n")[0]).upper())
                s += 0.2 if gl.line_total_cents == want["line_total_cents"] else 0
                if s > best_s:
                    best, best_s = j, s
            if best is None or best_s < 0.5:
                t.add("line found", False, L["id"], f"missing {wname!r}")
                wrong = True
                continue
            used.add(best)
            gl = got_lines[best]
            t.add("line found", True)
            check("line price", gl.line_total_cents, want["line_total_cents"])
            check("line kind", gl.kind, want["kind"])
            if want["kind"] == "item" and want["quantity"]:
                t.add("quantity/weight", abs((gl.quantity or 0) - want["quantity"]) < 1e-6, L["id"],
                      f"{wname}: expected {want['quantity']}, got {gl.quantity}")
        extra = len(got_lines) - len(used)
        t.add("no extra lines", extra == 0, L["id"], f"{extra} extra line(s)")

        expected_flag = L["expected_status"] == "needs_review"
        flagged = res.status == "needs_review"
        key = ("tp" if flagged else "fn") if expected_flag else ("fp" if flagged else "tn")
        flag[key].append(f"{L['id']} ({L.get('misprint') or L['degradation']}): "
                         f"{[i.code for i in res.issues if i.blocking]}")
        if wrong and not L.get("misprint"):  # extraction errors on a correctly printed receipt
            caught_errors["wrong"] += 1
            caught_errors["wrong_and_flagged"] += flagged

    # Layer 2: labels (from the committed demo labels) vs the generator's catalog
    truth = {}
    for norm, cat, _, _, names in CATALOG:
        for st in STORES:
            truth[(st.name, names[st.idx].upper())] = (norm, cat)
    labels = json.loads((ROOT / "demo" / "labels.json").read_text(encoding="utf-8"))
    by_product = defaultdict(set)
    conf = defaultdict(lambda: [0, 0])
    for l in labels:
        key = (l["store"], l["description_key"])
        if key in truth:
            norm, cat = truth[key]
            ok = l["category"] == cat
            t.add("category", ok, f"{l['store']} {l['description_key']}", f"expected {cat}, got {l['category']}")
            conf[l["confidence"] or "?"][0] += ok
            conf[l["confidence"] or "?"][1] += 1
            by_product[norm].add(l["normalized_name"])
    for norm, names in sorted(by_product.items()):
        t.add("one base name per product across stores", len(names) == 1, norm, f"names used: {sorted(map(str, names))}")
    # pack sizes: eggs carry pack_count in the answer keys
    egg_truth = {}
    for lp in labels_dir.glob("*.json"):
        L = json.loads(lp.read_text(encoding="utf-8"))
        if not (ROOT / "demo" / "extractions" / f"{sha256_file(ROOT / 'evals' / 'synthetic' / L['image'])}.json").exists():
            continue  # not extracted yet: its items were never labeled
        for x in L["lines"]:
            if x.get("pack_count"):
                egg_truth[(L["store"], x["printed"].upper())] = x["pack_count"]
    for (store, printed), n in sorted(egg_truth.items()):
        got = next((l for l in labels if l["store"] == store and l["description_key"] == printed), None)
        val = got and got.get("pack_base_qty")
        t.add("egg pack size", val is not None and abs(val - n) < 1e-6, f"{store} {printed}", f"expected {n}, got {val}")
    # every 12-egg carton printed without a count is fine; also check "12CT"-style names in the catalog
    return {"tally": t, "flags": flag, "caught": caught_errors, "confidence": dict(conf)}


# --- redaction ------------------------------------------------------------------------------------

def eval_redaction() -> dict:
    import io
    import re

    import pytesseract
    from PIL import Image, ImageOps

    from pricelens.redact import _ocr_words, load_image, prepare  # noqa: F401  (sets tesseract path)
    t = Tally()

    def ocr_text(img: Image.Image) -> str:
        gray = ImageOps.grayscale(img)
        if gray.width < 1200:
            gray = gray.resize((gray.width * 2, gray.height * 2))
        return re.sub(r"\s+", "", pytesseract.image_to_string(gray, config="--psm 6"))

    _ocr_words(Image.new("RGB", (10, 10), "white"))  # resolves the tesseract binary like the app does
    for lp in sorted((ROOT / "evals" / "synthetic" / "labels").glob("*.json")):
        L = json.loads(lp.read_text(encoding="utf-8"))
        img_path = ROOT / "evals" / "synthetic" / L["image"]
        original = load_image(img_path)
        prepared = prepare(img_path)
        held_back = not prepared.redaction_confident
        t.add("payment section blacked out (TOTAL found)", not held_back, L["id"],
              f"{L['degradation']}: TOTAL not found, image held back for review (not sent automatically)")
        before = ocr_text(original)
        after = ocr_text(Image.open(io.BytesIO(prepared.jpeg_bytes)))
        for v in L["sensitive_values"]:
            # All synthetic sensitive lines are printed below TOTAL (true by construction in the generator),
            # so a blacked-out payment section covers them even when OCR can't read them.
            protected = held_back or prepared.blacked_below is not None or v not in after
            t.add("sensitive values protected (blacked out, or image held back)", protected, L["id"],
                  f"{v} would be sent visible")
            if v in before:
                t.add("OCR check: sensitive value unreadable after redaction", v not in after, L["id"],
                      f"{v} still readable")
        if held_back:
            continue
        # Over-redaction, measured geometrically: does any black box, or the blacked-out area below TOTAL,
        # overlap a printed item price? (Comparing OCR text before/after was misleading: Tesseract reads the
        # whole page differently once boxes are added, e.g. it "lost" a clearly visible 2.09- far from any box.)
        words = _ocr_words(original)
        for x in L["lines"]:
            amount = f"{abs(x['line_total_cents']) // 100}.{abs(x['line_total_cents']) % 100:02d}"
            spots = [w for w in words if w.text.rstrip("-") == amount]
            if not spots:
                continue  # OCR didn't locate this price; can't judge it
            def hidden(w):
                return (w.top + w.height > prepared.blacked_below or
                        any(b.left < w.left + w.width and w.left < b.right and b.top < w.top + w.height and w.top < b.bottom
                            for b in prepared.boxes))
            # The same amount can be printed twice (a 2.00 coupon above TOTAL, "YOU SAVED 2.00" below it):
            # the price is only lost if EVERY printed copy is covered.
            covered = all(hidden(w) for w in spots)
            t.add("item prices not covered by redaction", not covered, L["id"], f"{amount} covered")
    return {"tally": t}


# --- CORD -------------------------------------------------------------------------------------------

def run_cord(budget: int, rpm: float) -> None:
    from pricelens.gemini import GeminiExtractor
    from pricelens.redact import prepare
    from pricelens.reliability import Budget, Caller, CircuitBreaker, RateLimiter, RetryPolicy, StopRun
    out = PRED / "cord"
    out.mkdir(parents=True, exist_ok=True)
    caller = Caller(RetryPolicy(), RateLimiter(rpm), Budget(budget), CircuitBreaker())
    ex = GeminiExtractor()
    todo = [p for p in sorted((ROOT / "evals" / "data" / "cord").glob("*.jpg")) if not (out / f"{p.stem}.json").exists()]
    print(f"{len(todo)} CORD receipts without a prediction; budget {budget}")
    for p in todo:
        try:
            res, attempts = caller.call(lambda: ex.extract(prepare(p)))
        except StopRun as e:
            print(f"stopped: {type(e).__name__}: {e}")
            break
        except Exception as e:  # recorded as a failed prediction: counts against accuracy, listed in the report
            (out / f"{p.stem}.json").write_text(json.dumps({"error": f"{type(e).__name__}: {e}"}), encoding="utf-8")
            print(f"{p.stem}: {type(e).__name__}")
            continue
        (out / f"{p.stem}.json").write_text(json.dumps({"model": res.model, "prompt_version": res.prompt_version,
                                                        "output": json.loads(res.raw_json)}, indent=1,
                                                       ensure_ascii=False), encoding="utf-8")
        print(f"{p.stem}: ok ({attempts} attempt(s))")
    print(f"requests sent: {caller.requests}, budget left: {caller.budget.left}")


def eval_cord() -> dict:
    from cord import load_truth, score
    t = Tally()
    caught = {"wrong": 0, "wrong_and_flagged": 0, "flagged": 0, "scored": 0}
    for gt in sorted((ROOT / "evals" / "data" / "cord").glob("*.json")):
        pred = PRED / "cord" / f"{gt.stem}.json"
        if not pred.exists():
            continue
        truth = load_truth(json.loads(gt.read_text(encoding="utf-8"))["gt_parse"])
        P = json.loads(pred.read_text(encoding="utf-8"))
        if "error" in P:
            t.add("receipt extracted", False, gt.stem, P["error"])
            continue
        t.add("receipt extracted", True)
        ext = ExtractedReceipt.model_validate(P["output"])
        # Score in rupiah whatever currency the model claimed (CORD is IDR); the app itself would flag it (V9).
        lines = [{"name": l.description or l.raw_text, "count": l.quantity, "price": cents(l.line_total, "IDR")}
                 for l in ext.lines if l.kind == "item"]
        s = score(truth, lines, cents(ext.subtotal, "IDR") if ext.subtotal else None, cents(ext.total, "IDR"))
        t.n["item name"] += s.items_expected
        t.k["item name"] += s.names_ok
        t.n["item count"] += s.counts_scored
        t.k["item count"] += s.counts_ok
        t.n["item price"] += s.prices_scored
        t.k["item price"] += s.prices_ok
        for f in s.failures:
            field = ("item name" if f.startswith(("name", "missing")) else "item count" if f.startswith("count")
                     else "item price" if f.startswith("price") else f.split(":")[0])
            t.fail[field].append(f"{gt.stem}: {f}")
        if s.subtotal_ok is not None:
            t.add("subtotal", s.subtotal_ok)
        if s.total_ok is not None:
            t.add("total", s.total_ok)
        # Does code verification catch the receipts the model got wrong? (V9 currency excluded: CORD is IDR.)
        ext_idr = ext.model_copy(update={"currency": "IDR"})
        res = verify(ext_idr, today=date(2030, 1, 1))  # CORD has no reliable dates: date checks ignored below
        # Arithmetic checks only (items vs subtotal vs total, line math, discount links). Missing fields (V6),
        # e.g. no store name in CORD's header-cropped photos, are reported separately: they aren't "errors caught".
        math_codes = {"V1", "V2", "V3", "V4", "V8"}
        codes = {i.code for i in res.issues if i.blocking}
        flagged = bool(codes & math_codes)
        wrong = bool(s.failures)
        caught["scored"] += 1
        caught["flagged"] += flagged
        caught["wrong"] += wrong
        caught["wrong_and_flagged"] += wrong and flagged
        caught["false_alarm"] = caught.get("false_alarm", 0) + (flagged and not wrong)
        caught["missing_field"] = caught.get("missing_field", 0) + ("V6" in codes)
    return {"tally": t, "caught": caught}


# --- report -----------------------------------------------------------------------------------------

def table(tally: Tally) -> str:
    lines = ["| Field | Result |", "|---|---|"]
    for f, k, n in tally.rows():
        lines.append(f"| {f} | {fmt(k, n)} |")
    return "\n".join(lines)


def failures(tally: Tally) -> str:
    out = []
    for f, items in tally.fail.items():
        if items:
            out.append(f"**{f}** ({len(items)}):\n" + "\n".join(f"- {x}" for x in items))
    return "\n\n".join(out) or "None."


def save(name: str, data: dict) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    t = data["tally"]
    payload = {"rows": t.rows(), "fail": dict(t.fail)} | {k: v for k, v in data.items() if k != "tally"}
    (RESULTS / f"{name}.json").write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")


def load(name: str) -> dict | None:
    p = RESULTS / f"{name}.json"
    if not p.exists():
        return None
    d = json.loads(p.read_text(encoding="utf-8"))
    t = Tally()
    for f, k, n in d["rows"]:
        t.k[f], t.n[f] = k, n
    for f, items in d["fail"].items():
        t.fail[f] = items
    d["tally"] = t
    return d


def report() -> str:
    parts = [f"# Eval results\n\nGenerated by `evals/run_eval.py`. Each number is correct/scored with a 95% Wilson "
             f"interval (small samples have wide intervals). Every failure is listed.\n"]
    syn = load("synthetic")
    if syn:
        f = syn["flags"]
        parts.append("## Synthetic receipts (fake, generated with answer keys)\n\nRead live by `gemini-3.6-flash`; "
                     "labels by `gemini-3.5-flash-lite`. Synthetic receipts are cleaner than real photos, so treat these "
                     "as an upper bound.\n\n" + table(syn["tally"]))
        parts.append(f"\n**Flagging (code verification):** {len(f['tp'])} of {len(f['tp']) + len(f['fn'])} receipts "
                     f"that should be flagged were flagged; {len(f['fp'])} of {len(f['fp']) + len(f['tn'])} clean "
                     f"receipts were flagged anyway.\n\nShould be flagged: " + "; ".join(f["tp"] + f["fn"]) +
                     (f"\n\nFalse alarms: {'; '.join(f['fp'])}" if f["fp"] else ""))
        c = syn["caught"]
        parts.append(f"\nReceipts with an extraction error that verification flagged: {c['wrong_and_flagged']}/{c['wrong']}.")
        parts.append("\n**Category accuracy by the model's own confidence:** " + ", ".join(
            f"{k}: {fmt(v[0], v[1])}" for k, v in syn["confidence"].items()))
        parts.append("\n### Synthetic failures\n\n" + failures(syn["tally"]))
    red = load("redaction")
    if red:
        parts.append("## Redaction (local OCR, 0 API calls)\n\nOCR reads each image before and after redaction. Recall counts "
                     "only values OCR could read in the original.\n\n" + table(red["tally"]) +
                     "\n\n**History:** the first version only boxed numbers OCR could read. On a heavily blurred receipt "
                     "OCR read none of them, so the card digits, auth code and member number were sent visible (59/66 "
                     "protected). Now everything below TOTAL is blacked out, and an image whose TOTAL can't be found is "
                     "held back for human review. Assumption: the generator prints all sensitive lines below TOTAL." +
                     "\n\n### Redaction failures\n\n" + failures(red["tally"]))
    cord = load("cord")
    if cord:
        c = cord["caught"]
        parts.append("## CORD-v2 (public real receipt photos, Indonesian rupiah)\n\nSeeded sample, `gemini-3.6-flash`. "
                     "Source: CORD-v2 (Clova AI / NAVER), https://huggingface.co/datasets/naver-clova-ix/cord-v2, CC BY 4.0. "
                     "CORD has no store/date labels.\n\n" + table(cord["tally"]) +
                     f"\n\n**Did the arithmetic checks catch the errors?** Of {c['wrong']} receipts with any extraction "
                     f"error, {c['wrong_and_flagged']} were flagged by the totals/line-math checks. False alarms (flagged, "
                     f"but read correctly): {c.get('false_alarm', 0)}/{c['scored']}. Separately, "
                     f"{c.get('missing_field', 0)}/{c['scored']} were flagged for a missing field (V6), usually the store "
                     f"name: CORD photos often crop the header, so the model correctly returned none."
                     "\n\n### CORD failures\n\n" + failures(cord["tally"]))
    text = "\n\n".join(parts) + "\n"
    (ROOT / "evals" / "RESULTS.md").write_text(text, encoding="utf-8")
    return text


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("what", choices=["synthetic", "redaction", "cord", "report"])
    ap.add_argument("--budget", type=int, default=15)
    ap.add_argument("--rpm", type=float, default=10)
    args = ap.parse_args()
    if args.what == "synthetic":
        save("synthetic", eval_synthetic())
    elif args.what == "redaction":
        save("redaction", eval_redaction())
    elif args.what == "cord":
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
        run_cord(args.budget, args.rpm)
        save("cord", eval_cord())
    print(report())


if __name__ == "__main__":
    main()
