# PriceLens — Plan

Status: **approved 2026-11-20** (all of section 7, redaction option C, plus a pre-computed public demo, section 4a). Layer 1 in progress.

Each design choice has a **Why** line, so it can be explained in an interview.
Items marked **[DECISION]** were approved on 2026-11-20.

---

## 0. Stack and dependencies [DECISION]

| Piece | Proposal | Why |
|---|---|---|
| Language | Python 3.13 (already installed) | Best-supported AI SDK, fast to test, common in data work. |
| AI model | Claude via the official `anthropic` SDK, model `claude-opus-5-5` | A vision model with **structured outputs**: the API returns JSON that already matches our schema, so we never parse free text. Opus 5.5 is the current default. Sonnet 5.5 or Haiku 5.5 would be cheaper, and the eval (section 5) can measure whether they are accurate enough. |
| Schema/validation | `pydantic` (installed with `anthropic`) | One schema works three ways: it is the model's output contract, the runtime validation, and the test fixtures. |
| Database | SQLite through the stdlib `sqlite3` module, with hand-written SQL and no ORM | No server and no extra dependency. The schema is visible in one `.sql` file, which helps when explaining it. |
| Images | `Pillow` | Strips EXIF/GPS data, downsizes images, draws redaction boxes. |
| Redaction | See section 3 for options **[DECISION]** | Hardest privacy requirement. |
| Config | `python-dotenv`, with `.env` gitignored | Keeps the API key out of code and out of git. |
| Tests | `pytest` | The standard Python test runner. |
| Web (Layer 3, later) | FastAPI + server-rendered HTML + one chart library from a CDN | Decided when Layer 3 starts; nothing to install yet. |

**Provider change (2026-11-21):** the default extractor is **Gemini (free tier), `gemini-3.6-flash`**, called through
the REST Interactions API with the stdlib `urllib`, so there's no new dependency. The Claude extractor stays as a second
implementation of the same interface. Google's terms for the free tier: inputs and outputs may be used to improve
Google products, and human reviewers may read them. Therefore:
- **Only public or fake images go to the free tier** (CORD-v2, synthetic, demo). This is **enforced in code**: an extractor with
  `trains_on_input=True` refuses any path outside `evals/data/cord`, `evals/synthetic` and `demo/receipts`, before
  any processing (`pricelens/policy.py`). `GEMINI_TIER` defaults to `free`, and only the exact value `paid` lifts the restriction.
- Requests send `store: false`, so Google doesn't keep the interaction for later turns.
- Store and date accuracy on real receipts waits until there's a no-training option (paid Gemini or Claude).
**Why:** a privacy rule that depends on remembering not to do something is easy to break by accident. One enforced in code isn't.

Dependencies requested for Layer 1: `anthropic`, `pillow`, `python-dotenv`, `pytest`, plus the redaction choice.

---

## 1. Layer 1 flow

```
pricelens add receipt.jpg
  1. hash image (sha256)       -> reject duplicates
  2. preprocess (local)        -> strip EXIF, fix rotation, downscale
  3. redact (local)            -> black out card/loyalty numbers
  4. extract (AI)              -> structured JSON (schema below)
  5. verify (code, no AI)      -> totals, per-line math, date, required fields
  6. store                     -> status = verified | needs_review
  7. print summary + any review reasons
pricelens review               -> list flagged receipts; approve / fix / reject
pricelens prices <text>        -> price history + changes for an item
```

### Extraction contract (what the model returns)

Per receipt: `store_name`, `date_raw` (exactly as printed), `date_iso`, `subtotal`, `tax`, `total`,
`currency`, `legibility` (`good` / `partial` / `poor`), and `lines[]`.
Per line: `raw_text`, `description`, `quantity`, `unit`, `unit_price`, `line_total`, `kind`, `section`,
`applies_to_line` (for discounts).

Prompt rules:
- **Transcribe the printed subtotal, tax, and total. Do not calculate them.**
  **Why:** if the model computed the subtotal, the code check would compare the model's arithmetic with itself and prove nothing. Comparing printed totals with extracted lines is an independent check.
- Return `null` for anything unreadable. Do not guess.
  **Why:** a null makes the check fail loudly, while a guess can pass silently.

Money is converted to **integer cents** in code, never stored as floats.
**Why:** `0.1 + 0.2 != 0.3` in floating point, and the totals check must be exact.

### Verification rules (pure code, unit-tested)

| # | Rule | On failure |
|---|---|---|
| V1 | Sum of `line_total` over all lines with `section='items'` (discounts negative) == printed subtotal | needs_review |
| V2 | subtotal + tax + sum of `section='after_subtotal'` lines (e.g. receipt-level coupons, bag fees) == printed total | needs_review |
| V3 | If no subtotal is printed: sum of items + tax == total (V1 is skipped and the receipt notes it) | needs_review |
| V4 | Per line: `quantity × unit_price` == `line_total` (±1¢ only for weighed items) | needs_review |
| V5 | Code re-parses `date_raw` and must get the model's `date_iso`. The date can't be in the future or >5 years old. Ambiguous dates (`03/04/26`) resolve with a configured locale (default US), and the receipt notes the ambiguity. | needs_review |
| V6 | Required fields are not null (store, date, total, at least 1 line) | needs_review |
| V7 | `legibility = poor` | needs_review |

Tolerance is **0¢** for V1–V3. **Why:** receipts are printed by a computer, so a 1¢ gap means a misread or a missing line, not rounding.

**"Flag, don't store silently":** a flagged receipt *is* saved, with `status='needs_review'`
and its reasons, but **all price history and comparisons only use `status='verified'`**.
`pricelens add` prints the failures. `pricelens review` lets you approve, correct, or reject.
**Why:** dropping a flagged receipt would waste the API call and your work. Saving it with a status keeps it out of the stats, and it can be fixed later.

**What the check does NOT catch:** consistent misreads that still add up, such as the wrong store name, wrong date, or a price swapped between two items. That's why the per-field eval (section 5) exists.

### Edge cases

| Case | Handling |
|---|---|
| Item discount ("SALE -0.50") | Separate line, `kind='discount'`, negative, `applies_to_line` → parent. Effective price = parent + its discounts. |
| Store coupon / member savings | `kind='coupon'`. In the items section if printed above the subtotal, otherwise `after_subtotal`. |
| Refund / return | `kind='refund'`, negative `line_total`, positive quantity. Excluded from price history. |
| Multi-buy ("2 @ 1.50 3.00") | quantity=2, unit_price=150, line_total=300. "2 for $5" deals: unit price = line_total / qty. |
| Weighed items ("1.32 lb @ 0.59/lb") | quantity=1.32, unit='lb', checked with a ±1¢ tolerance. |
| Date formats | Raw string kept; code parses a known list of formats (V5). |
| Blurry / crumpled | Model reports `legibility` and nulls; V6/V7 flag. No guessing. |
| Duplicate upload | Image sha256 is UNIQUE, so the second add is rejected. |
| Bottle deposit / bag fee | `kind='fee'`. Counted in totals, excluded from price history. |

---

## 2. Layer 1 schema (SQLite)

```sql
PRAGMA foreign_keys = ON;

CREATE TABLE stores (
  id            INTEGER PRIMARY KEY,
  name          TEXT NOT NULL,              -- display name, e.g. "Trader Joe's"
  name_key      TEXT NOT NULL UNIQUE        -- lowercased/trimmed for matching
);

CREATE TABLE receipts (
  id              INTEGER PRIMARY KEY,
  image_sha256    TEXT NOT NULL UNIQUE,     -- duplicate guard
  redacted_path   TEXT,                     -- local copy of what was sent to the API
  store_id        INTEGER REFERENCES stores(id),
  store_name_raw  TEXT,                     -- as printed
  purchase_date   TEXT,                     -- ISO 8601 'YYYY-MM-DD', NULL if unreadable
  date_raw        TEXT,                     -- as printed
  currency        TEXT NOT NULL DEFAULT 'USD',
  subtotal_cents  INTEGER,                  -- printed value; NULL if not printed
  tax_cents       INTEGER,
  total_cents     INTEGER,
  legibility      TEXT CHECK (legibility IN ('good','partial','poor')),
  status          TEXT NOT NULL CHECK (status IN ('verified','needs_review','rejected')),
  review_reasons  TEXT NOT NULL DEFAULT '[]',  -- JSON array of rule codes + messages
  model           TEXT NOT NULL,            -- e.g. claude-opus-5-5
  prompt_version  TEXT NOT NULL,            -- so eval results map to a prompt
  extraction_json TEXT NOT NULL,            -- full raw model output, for audit/re-verify
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  reviewed_at     TEXT
);

CREATE TABLE line_items (
  id               INTEGER PRIMARY KEY,
  receipt_id       INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
  line_no          INTEGER NOT NULL,        -- order on the receipt
  raw_text         TEXT NOT NULL,           -- exact printed line(s)
  description      TEXT,                    -- cleaned, still store-specific
  kind             TEXT NOT NULL CHECK (kind IN ('item','discount','coupon','refund','fee')),
  section          TEXT NOT NULL CHECK (section IN ('items','after_subtotal')),
  quantity         REAL,                    -- 2, or 1.32 for weighed
  unit             TEXT CHECK (unit IN ('each','lb','kg','oz','g')),
  unit_price_cents INTEGER,
  line_total_cents INTEGER NOT NULL,        -- negative for discounts/coupons/refunds
  applies_to_line  INTEGER,                 -- line_no of parent item (discounts)
  UNIQUE (receipt_id, line_no)
);

CREATE INDEX idx_receipts_store_date ON receipts(store_id, purchase_date);
CREATE INDEX idx_line_items_desc     ON line_items(description);
```

Design notes:
- **`extraction_json` is kept in full.** **Why:** if the verification rules change, every receipt can be re-verified without calling the API again, and any stored value can be traced back to what the model said.
- **`model` + `prompt_version` per receipt.** **Why:** when the prompt changes, eval results and old data can still be told apart.
- **`redacted_path` instead of storing the original image.** **Why:** the database only points to the image that was actually sent. The original stays wherever you keep it.
- **Layer 1 price history matches on `(store, description)`.** **Why:** text like "ORG BNNA 1LB" is consistent *within* one store. Comparing *across* stores needs the normalized names from Layer 2, which adds a `products` table and `line_items.product_id` in a migration later.
- Quantity is REAL because weights are fractional. Money is never REAL.

### Price-change logic (unit-tested)
For one `(store, description)`, take verified `kind='item'` lines ordered by date. Effective unit
price = (line_total + attached discounts) / quantity. Consecutive observations give
`change_cents` and `change_pct`. When there are several observations on the same day, use the lowest price.

---

## 3. Redaction before sending images [DECISION]

Goal: card digits and loyalty/member numbers never leave the machine.

| Option | How | Trade-off |
|---|---|---|
| **A. Local OCR (recommended)** | Tesseract (a separate Windows install, **not currently installed**) + `pytesseract`. Find words matching card patterns (`****1234`, `XXXX`, runs of ≥8 digits, lines with CARD/ACCT/MEMBER/LOYALTY/REWARDS/AUTH/APPROVAL) and black-box those whole lines. | Automatic. Adds a system install. Local OCR on crumpled receipts can miss text. |
| B. Manual crop | Before sending, the CLI shows the image and you give rectangles or crop the payment section off. | No dependency, but manual and easy to forget. |
| C. A + B | Run A, save a preview of the redacted image, and require you to confirm with `--yes` or by looking at the preview. | Safest, a little slower. |

Plus, always:
- Strip EXIF metadata (GPS location, device) and send only the redacted, downsized image, with no filename or user info.
- Post-check: if the model's output contains any run of ≥8 digits, mark the receipt needs_review and do not store that text.
- Measure redaction recall on the eval set (how many sensitive spans were caught) and report it honestly in the README.

My recommendation: **C**. A layered approach is easier to defend than one perfect method.

---

## 4. Layers 2 and 3 (outline; detailed plans come before each starts)

**Layer 2: categorization.** A fixed category list in code (e.g. produce, dairy, meat & seafood,
bakery, pantry, frozen, beverages, snacks, household, personal care, other) plus `uncertain`.
Structured output is restricted to that list, so the model can't invent categories. The cache table
is keyed by `(store_id, raw_text)` and the prompt version, so the same text is never sent twice.
Stored: raw text, normalized name, category, confidence, needs_review. Low confidence or `uncertain` sets the review flag.

**Layer 3: web.** Search, a results table, a per-product comparison across stores, and one price-over-time chart.
Manual corrections are saved and exported as regression test cases. Demo mode runs against a
separate SQLite file of **fake sample data**. The API key is server-side only, the demo is read-only, and
write endpoints exist only in local mode.

### Layer 4: reliability and systems (added 2026-11-20, built after Layer 3)

**Built into Layer 1 now** (cheap now, expensive to add later):
- `Extractor` interface (`extract(image) -> ExtractionResult`) with `ClaudeExtractor`, `ReplayExtractor` (saved
  responses, for the demo) and `FakeExtractor` (tests). The pipeline only sees the interface.
- One receipt plus all its lines saved in **one transaction**: all or nothing.
- sha256 of the original image is UNIQUE, so re-adding the same file is a no-op.
- Model output is validated against the Pydantic schema before anything is stored.
- Distinct error classes: `AuthError`, `RateLimitError`, `TransientAPIError`, `MalformedOutputError`,
  `UnreadableImageError`, `RefusalError`. A totals mismatch is not an exception: it's `status='needs_review'`.

**Built in Layer 4:**
- `pricelens batch <folder> --workers N --rpm R`: a thread pool, because the work is waiting on the network,
  not the CPU. Workers only extract and verify. **All database writes go through the main thread**, because SQLite
  allows one writer at a time and this avoids "database is locked" errors. A shared rate limiter keeps requests under R per minute.
- Own retry wrapper (SDK `max_retries=0`), so the policy is explicit and unit-testable: exponential backoff
  with jitter, honors `retry-after`, a maximum number of attempts, retries only timeouts, 429s, 5xx and connection errors. Auth errors, bad requests and
  unreadable images fail immediately.
- `ingest_runs` + `ingest_failures` tables (an additive migration). `retry-failed` reprocesses only the failures,
  and every run ends with a succeeded / failed / flagged summary.
- Likely-duplicate detection: same store + date + total but a different image hash, so it is flagged for review, not dropped.
- `pricelens verify`: re-checks stored rows (totals, missing fields, orphaned line items, missing redacted
  images) and reports mismatches.
- Tests: retry policy (fake clock), duplicate detection, transaction rollback, a batch where the fake
  extractor fails on some receipts, and re-running a batch inserts nothing new.
- README "Design tradeoffs": concurrency level, retry policy, storage.

### 4a. Pre-computed public demo (approved)

Anyone can use the demo without logging in, with **no live AI calls and no uploads**:
1. Synthetic receipts (fake stores, fake prices) run once through the real pipeline locally.
2. Each raw model response is saved as a fixture (`demo/extractions/<sha256>.json`) and committed.
3. The demo database is built from those fixtures by the *same* verify + store code, through a
   **replay extractor**. It returns the saved response for an image instead of calling the API.
4. Visitors browse the receipts and see each receipt image next to its extracted JSON and the verification
   result (including deliberately flagged receipts), then search, compare, and view the chart.

**Why:** the public site has no API key, so nobody can run up costs or abuse it, and nothing private
can be uploaded. Every demo result is real model output, not hand-written data. The replay extractor
also makes tests and re-verification free and repeatable. Layer 1 therefore builds the extractor as an interface with
`live` and `replay` implementations.

---

## 5. Testing and evals

- **Unit tests** (no API calls): V1–V7, cents conversion, date parsing, price-change math,
  redaction pattern matching. All edge cases from section 1 become fixtures.
- **Eval set: 15–20 receipts** with hand-labeled JSON (`evals/labels/*.json`). The run reports
  accuracy per field: store, date, subtotal/tax/total, line-item prices (matched by line), later
  categories. It also reports the verification-flag rate, redaction recall, and **lists every failure**, not just the averages.
- **CORD-v2 (added 2026-11-20)**: public Indonesian receipts with labels
  ([naver-clova-ix/cord-v2](https://huggingface.co/datasets/naver-clova-ix/cord-v2), CC BY 4.0).
  Used for **item name, count, price, subtotal, total** on the validation + test splits. CORD has no store
  or date labels, so those fields are evaluated only on the private and synthetic sets below.
  - `evals/download_cord.py` uses the Hugging Face datasets-server JSON API with the stdlib `urllib`, so
    there's **no new dependency**. Images go to `evals/data/cord/` (gitignored, never committed).
  - Label normalization: strip `Rp`/`Rp.`, spaces and thousands separators (`25,000` / `25.000` / `Rp 25.000`).
    `menu` can be a single object or a list. Items are matched by name similarity + price.
  - Money is stored in the currency's **minor unit** (cents for USD, whole rupiah for IDR, which has no
    minor unit). The `*_cents` column names mean "minor units". **Why:** one integer type works for every currency
    and stays exact.
  - Any CORD image shown in the README or demo is credited: "CORD-v2, naver-clova-ix, CC BY 4.0" with a link.
  - Running the model on all ~200 receipts costs real money. I'll estimate the cost and ask before running.
- **[DECISION] Where the eval receipts come from:**
  - Real receipts (best signal) contain private data, so the images and labels stay **out of git**
    (`evals/private/`, gitignored), and only aggregate results go in the README.
  - Synthetic receipts (rendered by a script, with fake stores and edge cases) are committed and
    double as the demo data, but they are easier than real photos.
  - Proposal: ~12 real (private) + ~6 synthetic (public), reported separately.

---

## 6. Repo layout (proposed)

```
pricelens/            package: cli.py, extract.py, verify.py, redact.py, db.py, prices.py, schema.sql
tests/                unit tests
evals/                runner, synthetic receipts + labels; private/ gitignored
data/                 local DB + redacted images (gitignored)
.env.example          ANTHROPIC_API_KEY=   (real .env gitignored)
```

---

## 7. Decisions needing your approval

1. Stack and dependencies (section 0): Python, `anthropic`, `pillow`, `python-dotenv`, `pytest`.
2. Model: `claude-opus-5-5`, or start cheaper and let the eval decide.
3. Redaction option A, B, or **C** (section 3), and whether you're OK installing Tesseract.
4. Flagged receipts are stored with `needs_review` and excluded from stats (rather than not stored at all).
5. Schema (section 2).
6. Eval source mix (section 5).
