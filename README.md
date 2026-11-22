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
  per gal (imperial, default) or per 100 g / 100 ml (metric), so a 30-pack and a 6-pack compare fairly, and switching
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
| V9 | currency is USD or CAD (anything else is flagged; USD and CAD are never compared) |
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

## Reliability (Layer 4)

```bash
.venv\Scripts\python -m pricelens batch evals\synthetic\receipts --workers 2 --rpm 10 --budget 15
.venv\Scripts\python -m pricelens retry-failed      # only the retryable failures, e.g. after a daily-quota stop
.venv\Scripts\python -m pricelens verify            # re-check everything stored; reports, fixes nothing
```
A run ends with a summary like `19 verified | 2 flagged | 1 failed | 0 already stored | 22 requests (0 retries)`.

### Design tradeoffs
- **Concurrency: threads, 2 workers, one writer.** The work is waiting on the network (and Tesseract runs as a
  subprocess), so threads are enough and simpler than processes. Workers redact, extract and verify; **only the main
  thread writes to SQLite**, because SQLite allows one writer and this rules out "database is locked" races. Each receipt
  is still its own transaction. The default is 2 workers at 10 requests/min, because on the free tier the
  bottleneck is the **daily** quota (20/day), not speed. A test runs 8 workers on 40 receipts and checks every write
  came from one thread.
- **Retry policy: retry only what can succeed.** Timeouts, connection errors, 5xx and per-minute 429s are retried with
  exponential backoff and full jitter (max 4 attempts, honoring the server's wait hint). Auth errors, 400s, refusals,
  unreadable images and malformed output fail immediately: retrying the same input gives the same answer.
  A **daily-quota** 429 stops the run (retrying for 15 hours is pointless), and `retry-failed` resumes later.
- **Circuit breaker, from a real incident.** During a 45-minute Gemini outage, my first retry loop sent 24 requests
  into it. Now 3 consecutive failures pause the run, one probe is sent, and if that fails the run stops with a clear message.
  A test replays the incident: 4 requests instead of 24.
- **Budget:** `--budget` caps requests per run, retries included, because failed attempts may count against quota.
- **Storage:** SQLite with plain SQL and numbered migrations: zero setup, one file, and the schema history is
  readable. The trade-off is a single writer, which the one-writer design above works around. A multi-user
  deployment would move to Postgres.
- **Idempotency:** the image hash is `UNIQUE`, so re-running a batch inserts nothing twice and makes no API calls for
  stored images. Two *different* photos of the same receipt (same store, date and total) are flagged `D1`, never
  silently merged.

## Eval results

Full report, with 95% intervals and every failure listed: [evals/RESULTS.md](evals/RESULTS.md).
Run it with `python evals/run_eval.py synthetic|redaction|cord|report`.

**Synthetic receipts** (21 fake receipts with answer keys, read live by `gemini-3.6-flash`). These are clean,
generated images, so treat them as an upper bound, not as real-world accuracy:

| Field | Result |
|---|---|
| store, date, subtotal, tax, total | 21/21 each |
| line items found / price / kind (item, discount, coupon...) | 147/147 each |
| category (17 fixed categories) | 63/63 |
| same base name for a product at every store | 25/25 |
| egg pack sizes | 6/6 (95% CI 61–100%: too few to say much) |
| **misprinted receipts flagged by code** | **2/2, with 0 false alarms on 19 clean receipts** |

The model reported "high" confidence on 61 of 63 labels, so self-reported confidence carried almost no signal here.

**Redaction** (local OCR before and after, 0 API calls):
- **Sensitive values protected: 66/66.** 21 of 22 receipts had everything below TOTAL blacked out. The heavily
  blurred one was held back for review instead of being sent.
- **Item prices untouched by redaction: 117/117.**
- *How it got there:* the first version only boxed numbers OCR could read, which gave **59/66**. On the heavily blurred
  receipt it found none of the card, auth or member numbers and sent them visible, while the model read the rest of
  that receipt perfectly. The first gate then let a noisy receipt through because one line was boxed, while the card
  digits next to it weren't. Now only a located TOTAL counts as safe. Both cases are regression tests.

**CORD-v2** (30 real receipt photos, public benchmark): runs over two days on the free tier's 20 requests/day.
Results will appear here.

*Measurement lessons:* two of my first metrics were wrong, and the eval was fixed, not the numbers. "Blurry receipts
should be flagged" was a bad expectation (the model read the blurry one correctly). And comparing OCR text
before and after redaction blamed redaction for OCR noise. Overlapping redaction boxes against printed
prices measures it directly.

## Data sent to the model, and how it's protected

- **Only a redacted, re-encoded JPEG is sent**, plus a fixed prompt. No filename, user info, or metadata.
- **Local redaction first, in two layers** (Tesseract OCR on your machine):
  1. **Everything below the TOTAL line is blacked out.** Card, auth, change and loyalty lines are printed
     there, and nothing below TOTAL is needed for extraction. Finding the word "TOTAL" is much easier for OCR than
     reading a blurry card number. Noisy photos get a second, denoised OCR pass if the first misses it.
  2. **If TOTAL can't be found, the image is held back.** `batch` and `add --yes` refuse to send it (recorded for
     manual review). Interactive `add` shows the preview and requires typing `send`. An override exists only for
     the public/fake data folders.
  Masked card numbers, long digit runs and numbers on CARD/ACCT/MEMBER/AUTH lines anywhere else are also boxed.
  Re-encoding drops EXIF/GPS.
- **Backstop:** if card- or loyalty-like numbers still appear in the model output, the receipt is flagged
  and the text is replaced with `[REDACTED]` before storage.
- **Provider policy, enforced in code:** the default provider is the free Gemini API tier, where Google
  may use inputs to improve its products and human reviewers may read them. So the free tier **only accepts
  images from the public/fake data folders** (`evals/data/cord`, `evals/synthetic`, `demo/receipts`).
  Real receipts are refused before any processing. Requests also send `store: false`.
- **Keys:** API keys live in `.env` (gitignored) and are only read server-side.

## Known limitations
- Redaction relies on OCR finding the TOTAL line. When it can't, the receipt is held back rather than guessed at,
  so very poor photos need a human decision. It also assumes payment details are printed below TOTAL. A receipt
  with a card number *above* TOTAL is only protected by the per-line rules.
- Free Gemini tier: **20 requests/day per model**. Categorization batches per store, so 18 receipts take 3 calls.
- Items from real (private) receipts are not categorized on the free tier, by design.
- Varieties aren't yet normalized across stores ("boneless" vs "boneless skinless", "80%" vs "80/20").
  Base names are, which is what comparison uses.
- Canadian receipts print "$" too. The model decides CAD vs USD from tax names (GST/HST/PST) and addresses, and
  can get it wrong. A wrong currency still passes the totals checks.
- A pack size is only used if its number is printed: "WHOLE MILK GAL" has no number, so milk falls back to
  per-package comparison.
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
