# PriceLens

Photograph a store receipt, use a vision AI model to extract the store, date, and line items, and
**verify the result with code** before it enters a price history database. Then search a product
and compare its price across stores and over time.

> Status: **Layers 1-3 done** (extraction + verification, categorization, web interface). Layer 4 and the
> full eval are next.
> See [PLAN.md](PLAN.md) for the full plan and the reasoning behind each decision.

## What it does

- `pricelens add receipt.jpg`: redacts the image locally, extracts structured JSON with a vision model,
  verifies it with code, and stores it in SQLite with the raw printed line text.
- `pricelens review ...`: lists flagged receipts and lets you approve or reject them, or export the JSON, fix it, and
  import it, which re-runs verification.
- `pricelens prices <item>`: price history and increases/decreases, from **verified receipts only**.
- `pricelens categorize`: assigns each item a category from a **fixed list of 17** (or `uncertain`), a
  base product name for cross-store comparison ("BNNA ORG" -> `bananas`), and a variety (`organic`).
  Results are cached per store and printed name, so the same text is never sent twice.
- **Unit prices:** the AI reads pack sizes from printed names ("LG EGGS 24CT" -> 24, "EVOO 16.9OZ" -> 16.9 fl oz),
  and code checks that the number is actually printed before using it. Products are compared per egg / per lb /
  per fl oz (imperial, default) or per 100 g / 100 ml (metric), so a 30-pack and a 6-pack compare fairly, and switching
  pack size never counts as a price change. In the live run the check rejected 7 made-up sizes (scale weights).
- `pricelens label list|fix`: review flagged labels and correct them. Human corrections are never overwritten
  by the AI and are saved as regression test cases.
- Handles item discounts, coupons, refunds, multi-buys ("2 @ 1.50", "3 for $5"), weighed items,
  missing subtotals, multiple tax lines, many date formats, and currencies with no minor unit (IDR).

## Web interface

```bash
.venv\Scripts\python -m pricelens demo build              # rebuild data/demo.db from committed files (0 API calls)
.venv\Scripts\python -m pricelens serve --mode demo       # http://127.0.0.1:8000, fake data, read-only
.venv\Scripts\python -m pricelens serve                   # local mode: your database, correction forms
```

- **Products:** search, plus the lowest current price and how many stores sell it.
- **Product page:** latest price per store (with variety), the price-over-time chart, price changes, and every purchase.
- **Receipts:** the redacted image next to the extracted lines and the result of each check.
- **Labels:** AI labels with review flags. In local mode each row has a correction form, and corrections become regression cases.

**Demo mode is read-only twice over:** the write routes aren't registered, *and* the SQLite connection is opened
`mode=ro`, so even a bug can't write. It needs no API key. It serves only images from `demo/images`, looked up by hash.
The chart is inline SVG drawn in Python (no JS, strict CSP: `default-src 'none'`).

## Quick start

```bash
python -m venv .venv
.venv\Scripts\python -m pip install pillow python-dotenv pytesseract pytest anthropic   # anthropic only for --provider claude
copy .env.example .env          # then add GEMINI_API_KEY (from https://aistudio.google.com/apikey)
# Install Tesseract OCR (used for local redaction), e.g. on Windows:
winget install UB-Mannheim.TesseractOCR

.venv\Scripts\python -m pytest                       # unit tests, no API calls
.venv\Scripts\python -m pricelens add evals\synthetic\smoke_freshway.png
.venv\Scripts\python -m pricelens review list
.venv\Scripts\python -m pricelens prices yogurt
```

## How the AI output is verified

The model is never trusted to check itself. It **copies** the printed subtotal, tax and total and is told
never to calculate them. Code then checks the line items against those printed numbers:

| Rule | Check |
|---|---|
| V1 | line items (discounts negative) sum **exactly** to the printed subtotal |
| V2 | subtotal + tax + post-subtotal coupons/fees == printed total |
| V3 | no subtotal printed: items + tax == total (and a note that V1 couldn't run) |
| V4 | quantity × unit price == line total (rounding tolerance of at most qty × ½¢) |
| V5 | code re-parses the printed date and must agree with the model; no future or >5-year-old dates; ambiguous dates are noted |
| V6/V7 | required fields present; nothing unreadable; model didn't report poor legibility |
| V8 | every discount points at a real item line |
| S1 | no card- or loyalty-number-like text in the output (scrubbed before storage) |

Money is converted from the printed string to integer minor units in code, never stored as floats.
A receipt that fails any rule is **stored with `status = needs_review`** and kept out of all price
history until a human approves or corrects it.

**Example (live run, Gemini, fake receipt):** the printed subtotal was $16.95 but the items add to
$15.95. The model copied the printed value as instructed, and V1 flagged the $1.00 gap. If the model had
calculated the subtotal itself, this would have passed silently.

**What this cannot catch:** misreads that still add up, such as the wrong store name, the wrong date, or two prices swapped
between items. That is what the per-field eval is for.

### Verifying categorization (Layer 2)
- Categories are an `enum` in the output schema, so the model can't invent one.
- Code checks every batch: exactly the item ids sent come back, each once, or the whole batch is rejected.
  Names can't contain digits, sizes or prices. `uncertain` or below-high confidence goes to review.
- **Self-reported confidence turned out to be useless here:** the model said "high" for all 63 labels,
  including a wrong one ("CHEDDAR BLK", meaning block, became "black cheddar"). So accuracy is measured against labels,
  not trusted from confidence.
- **Preliminary live check** (18 synthetic receipts, `gemini-3.5-flash-lite`): categories 62/62; every
  product got one base name across all three stores (25/25). The first prompt version gave 39/62 exact names,
  mostly from inconsistent specificity ("whole milk" vs "milk"). Splitting **base name + variety** and sending
  the model the names already in use fixed that.

## Eval results
_TODO: CORD-v2 (item name/count/price, subtotal, total) + synthetic receipts (store, date). Per-field
accuracy, flag rate, redaction recall, and every failure listed._

## Data sent to the model, and how it's protected

- **Only a redacted, re-encoded JPEG is sent**, plus a fixed prompt. No filename, user info, or metadata.
- **Local redaction first:** Tesseract OCR runs on your machine and blacks out masked card numbers, long
  digit runs, and numbers on CARD/ACCT/MEMBER/LOYALTY/AUTH lines (prices on those lines are kept).
  Re-encoding drops EXIF/GPS. You see the exact image that will be sent and must confirm it (or pass `--yes`).
- **Backstop:** if card- or loyalty-like numbers still appear in the model output, the receipt is flagged
  and the text is replaced with `[REDACTED]` before storage.
- **Provider policy, enforced in code:** the default provider is the free Gemini API tier, where Google
  may use inputs to improve its products and human reviewers may read them. So the free tier **only accepts
  images from the public/fake data folders** (`evals/data/cord`, `evals/synthetic`, `demo/receipts`).
  Real receipts are refused before any processing. Requests also send `store: false`.
- **Keys:** API keys live in `.env` (gitignored) and are only read server-side.

## Known limitations
- OCR-based redaction can miss text on crumpled or low-contrast receipts, which is why there's a human preview.
- Free Gemini tier: **20 requests/day per model**. Categorization batches per store, so 18 receipts take 3 calls.
- Items from real (private) receipts are not categorized on the free tier, by design.
- Varieties aren't yet normalized across stores ("boneless" vs "boneless skinless", "80%" vs "80/20").
  Base names are, which is what comparison uses.
- Product granularity is a judgment call. "cheese / cheddar block" and "cheddar cheese" are both defensible.
- Ambiguous numeric dates (03/04/26) are resolved by a configured locale (default US).
- Tested live on synthetic receipts only so far. Accuracy numbers come with the eval.

## Design decisions
See [PLAN.md](PLAN.md). Each choice there has a one-line "Why".

## Project layout
```
pricelens/   cli, pipeline, extract (Claude/replay/fake), gemini, redact, verify, money, dates, prices, db, policy
tests/       unit tests (no network)
evals/       synthetic receipts; CORD download + eval runner (next)
```
