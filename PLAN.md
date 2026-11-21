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

**Layer 2: categorization (approved and built 2026-11-21)**

*Fixed category list* **[DECISION]**. It lives in code, and the structured-output schema uses it as an
`enum`, so the model cannot invent a category:

| key | covers |
|---|---|
| `produce` | fruit, vegetables, fresh herbs |
| `meat_seafood` | fresh/packaged meat, poultry, fish |
| `dairy_eggs` | milk, cheese, yogurt, butter, eggs, plant milks |
| `bakery` | bread, buns, pastries, tortillas |
| `deli_prepared` | deli meats, rotisserie chicken, ready meals |
| `frozen` | frozen foods of any kind |
| `pantry` | dry goods, canned, pasta/rice, oils, condiments, spices, baking |
| `snacks_sweets` | chips, crackers, candy, cookies |
| `beverages` | water, soda, juice, coffee, tea (non-alcoholic) |
| `alcohol` | beer, wine, spirits |
| `household` | cleaning, paper goods, bags, kitchen supplies |
| `personal_care` | toiletries, cosmetics |
| `health` | medicine, vitamins, first aid |
| `baby_pet` | baby and pet products |
| `restaurant_food` | food/drink ordered at a restaurant or café (most CORD receipts) |
| `other` | clearly identifiable but none of the above |
| `uncertain` | the model can't tell what the item is (e.g. "ITM 40017 GV") |

`other` and `uncertain` are different on purpose. `other` means "I know what this is and it doesn't fit".
`uncertain` means "I don't know what this is". Only `uncertain` is a reading problem.

*What the model returns per item:* `normalized_name` (generic, lowercase, no brand, size or
codes: "ORG BNNA 1LB" → "bananas"), `category`, `confidence` (`high` / `medium` / `low`).

*Verification in code:*
- The model must return exactly the item ids it was sent: none missing, none extra, no duplicates.
- `normalized_name` must be non-empty, have no prices, digits-only tokens or SKU codes, and be at most 40 characters.
- `needs_review` is set when the category is `uncertain`, confidence isn't `high`, or a name check fails.
- **Why categorical confidence, not a 0–1 score:** models' self-reported probabilities are poorly calibrated.
  Three buckets are honest about that, and the eval reports **accuracy per bucket**, which shows whether
  "high" actually means anything.

*Cache (no repeat calls):* one row per `(store, description_key)` in a new `item_labels` table.
`description_key` is the printed item name (`description`, not `raw_text`), uppercased with collapsed spaces.
**Why not `raw_text`:** it contains the price ("MILK 1GAL 3.49"), so every price change would miss the cache.
**Why include the store:** abbreviations are store-specific ("GV" means Great Value at one store and something else at another).

*Batching:* all the uncached items from a receipt go in **one text-only request**, not one request per item.
**Why:** it uses fewer calls under free-tier rate limits, and the id check above catches dropped or invented items.

*Human corrections:* `pricelens label fix <store> "<description>" --name ... --category ...` writes the row with
`source='human'`. AI results **never overwrite human rows**. Corrections are also appended to a JSONL file of
regression test cases, which the eval re-runs to check the model still gets them right.

*Cross-store comparison:* `prices` searches `normalized_name`, so "bananas" matches "ORG BNNA 1LB" at one store
and "BANANAS" at another.

*Privacy (follows from your Gemini decision)* **[DECISION]**: item names from your real receipts are personal
data (a shopping history), so they must not go to the free tier either. Each receipt gets a `source` column
(`public` / `private`), set from its image path when it's added. A categorizer with `trains_on_input=True` only
receives items from `public` receipts. Private items stay uncategorized until a no-training provider is configured.

*Schema migration:* adds `item_labels` and `receipts.source`, and backfills `line_items.description_key`.
It uses SQLite's `PRAGMA user_version` with numbered migration files, which Layer 4 needs anyway.

*Model (2026-11-21):* the free tier allows **20 requests/day per model** (found live). Extraction stays on
`gemini-3.6-flash`. Categorization uses `gemini-3.5-flash-lite`: it's a simpler text-only task and has its own quota,
and the eval measures whether it's accurate enough. Requests are **batched per store per run** (not per receipt):
18 receipts take 3 calls instead of 18.

*Tests:* the id-mismatch check, the name checks, the review-flag rules, the cache (a second run makes zero
calls), human corrections winning over AI, the privacy filter, the migration on a Layer 1 database, and the
cross-store price query. A live run on the synthetic receipts. Category accuracy goes into the eval with a
hand-labeled set (CORD has no category labels).

**Layer 3: web interface (approved and built 2026-11-21)**

*As built:* the form body is parsed with the stdlib (`urllib.parse`) instead of adding `python-multipart`, which FastAPI's `Form()` needs.

*Stack* **[DECISION]**: **FastAPI + Jinja2 templates + uvicorn**, server-rendered HTML with a small amount of CSS
and no front-end build step. Tests use FastAPI's `TestClient`, which needs `httpx` as a dev dependency.
**Why:** server-rendered pages are easy to make secure (Jinja escapes everything by default), easy to test
(plain HTTP and HTML), and there's no JS framework to explain. FastAPI is widely used, typed, and documents
its own routes. (Alternative: Flask, with slightly fewer dependencies and a built-in test client.)

*Pages*
| URL | What it shows |
|---|---|
| `/` | Search box and a results table: product, varieties, category, number of stores, lowest latest price |
| `/product/{name}` | Latest price per store (cheapest first, variety shown), **the price-over-time chart** (one line per store), and price changes |
| `/receipts` and `/receipts/{id}` | The redacted image next to the extracted lines and the verification result, so you can see *why* a receipt was flagged. This page shows the "verified AI output" story directly. |
| `/labels` | Every label, with review flags. In local mode each row has a correction form. |

*Chart* **[DECISION]**: **inline SVG drawn in Python**, with no charting library and no JavaScript.
**Why:** one simple line chart doesn't justify a JS dependency or a CDN. It also works with JavaScript off,
can be unit-tested (the SVG is just text), and is safe under a strict Content-Security-Policy.

*Corrections as test cases:* the `/labels` form calls the same `fix_label()` as the CLI, so it has the same
validation, never overwrites a human correction with AI, and appends a JSONL regression case. A small page or
CLI command re-checks the current labels against all saved cases.

*Two modes*, chosen by `PRICELENS_MODE`:
| | `local` (default) | `demo` (public) |
|---|---|---|
| Database | `data/pricelens.db`, read-write | `data/demo.db`, opened **read-only** (`?mode=ro`) |
| Correction forms / POST routes | yes | **not registered at all** |
| API key | may be in `.env` (the web app never calls the API anyway) | **not needed or loaded** |
| Listens on | `127.0.0.1` only | host/port set by the host platform |

Defense in depth for "no public write access": the write routes don't exist in demo mode, *and* the
database connection itself refuses writes. A test checks both.

*Building the pre-computed demo:* `python -m pricelens demo build` rebuilds `data/demo.db` from committed files only:
- receipt extractions are replayed from `demo/extractions/` through the real verify-and-store code;
- labels come from a new committed `demo/labels.json`, exported from the live categorization run.
It makes zero API calls and needs no key, so anyone can reproduce the demo from the repo.

*Security details:* Jinja autoescaping (with a test that searches for `<script>`), parameterized SQL only, a strict
CSP header (no inline scripts, which the SVG chart doesn't need), POSTs checked for a same-origin `Origin`
header, and images served only from the redacted-images folder by hash, never by user-supplied path.

*Public hosting* is **not** part of this layer. The app runs locally in either mode. Where to host the
demo is a separate, later decision.

*Tests:* search results and escaping, product comparison numbers, the SVG chart (points and axes from known data),
receipt page issues, a label correction through the form (creates a regression case), demo mode (POST routes
return 404/405, the read-only database rejects writes even when called directly, and it works with no API key), and
the demo build being deterministic.

### Pack sizes and unit prices (approved and built 2026-11-21)

*Decided:* imperial by default (per each / per lb / per fl oz), switchable to metric (per 100 g / 100 ml) with a
cookie in the web app and `--units` / `PRICELENS_UNITS` in the CLI. Internally: count, g, ml.
*As built:* the live run showed the model taking scale weights ("1.07 lb @ 0.59/lb") as pack sizes. The P1 check
rejected all 7 (flagged, not used). A prompt fix ("a weight from a scale line is not a pack size") is next, when quota allows.
Also fixed: V4 now rounds half-up like a register and gives weighed items 1 cent of tolerance, as planned.

*Problem (found with the egg-pack test receipts):* "6 eggs $2.29" and "30 eggs $8.99" both count as
"eggs, $ each" today. So the 6-pack is badged "cheapest", and a store switching pack size looks like a price change.

*Proposal: unit prices, like a shelf tag.*
- The categorizer also returns `pack_quantity` and `pack_unit` (`count`, `g`, `ml`, or null) from the printed name:
  "LG EGGS 24CT" → 24 count; "EGGS 1/2 DOZEN" → 6 count; "SPAG 16OZ" → 454 g; "MILK 1GAL" → 3785 ml; "AVOCADO" → null.
- **Code check (P1):** the quantity must be backed by the printed text. The digits must appear ("24", "16OZ", "1GAL"),
  or it must come from a known word ("DOZEN" = 12, "1/2 DOZEN" / "HALF DOZEN" = 6). Conversions (oz→g, gal→ml) are
  done in code, not by the model. If the check fails, the pack size is ignored and the label is flagged for review.
- Stored on `item_labels`, so it's cached with the label: "EGGS 18CT" is always 18.
- **Unit price** = effective price ÷ pack quantity, shown per egg, per 100 g or per 100 ml **[DECISION: metric
  display, or also per lb/oz?]**. Weighed items keep their per-lb price.
- **Cross-store comparison and "cheapest"** use unit price when every compared row has a known pack size.
  Otherwise they fall back to per-package prices with a "pack sizes differ" note.
- **Price changes** are tracked per (store, product, variety, **pack size**), so 12-pack → 18-pack isn't a "change".
  The chart plots unit price.
- *Eval:* the generator's labels already carry `pack_count` for eggs, so pack-size accuracy is measured too.

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

**Detailed plan (approved and built 2026-11-21), sharpened by what went wrong in today's live runs:**

*As built:* `pipeline.process()` (no DB, runs in workers) / `pipeline.store()` (main thread only); `reliability.py`
(Caller = budget + rate limiter + circuit breaker + retry policy); `batch.py`; `audit.py` (`pricelens verify`);
migration 004. Replays (`--replay`) skip the budget and rate limit (no network).

| Today's problem | Layer 4 answer |
|---|---|
| A 45-minute Gemini outage (503) and my retry loop kept hammering it | **Circuit breaker:** after 3 consecutive transient failures across workers, the batch pauses everything (5 min), sends one probe, and gives up with a clear message if the probe fails. No blind retrying into an outage. |
| The daily limit (20/day) said "retry in 15h9m" in the message body, not a header | Parse that hint. A **daily-quota error is not retried**: the batch stops cleanly and `retry-failed` resumes tomorrow. Only per-minute limits are waited out. |
| Failed attempts may count against the daily quota | `--budget N`: the maximum number of requests a run may send (default 15). The run plans within it and reports what's left over. |
| Editing code during a background run broke 15 extractions | (A process lesson, not code.) Batches are resumable, so an interrupted run loses nothing: `retry-failed` continues. |
| Categorize kept going after a rate limit | Categorize goes through the same retry, budget and circuit-breaker policy. |

*Concurrency level* **[DECISION]**: default **`--workers 2 --rpm 10`**. **Why:** on the free tier the bottleneck is the
daily quota, not speed. 2 workers overlap a little network waiting without bursting into rate limits. The pool,
limiter and single writer are built properly, so a paid tier can raise the numbers without code changes. The tests run with 8 workers
to prove there are no races.

*Retry policy (one tested function, fake clock):* timeouts, connection errors, 5xx and per-minute 429 → retry with
exponential backoff and full jitter (base 2 s, cap 60 s, **max 4 attempts**), honoring `retry-after`. Auth errors,
400s, refusals, unreadable images and malformed output → **fail immediately**. Malformed output isn't retried, because
the same input would most likely give the same bad output, and it's recorded for review.

*Failure records:* `ingest_runs(id, started, finished, budget, requests_sent, status)` and
`ingest_failures(run_id, image_path, sha256, error_type, message, attempts, retryable)`. `retry-failed` reprocesses only
the latest unresolved failures. The end-of-run summary looks like `12 verified · 2 flagged · 1 failed (RateLimit: daily quota) · 3 already stored · 9 requests left`.

*Likely duplicates:* same store + date + total, but a different image hash (e.g. two photos of one receipt) → stored
as `needs_review` with reason `D1`, never silently dropped or merged.

*`pricelens verify`:* re-runs the V-rules on every stored receipt from `extraction_json` (catches rule changes) and
checks for orphaned line items, labels pointing at missing stores, missing redacted images, and receipts whose
status no longer matches their checks. It reports, and fixes nothing automatically.

*No new dependencies:* `concurrent.futures`, `threading` and `time` only.

### Layer 5: expense splitter (added 2026-11-21; built after Layer 4 + eval; detailed plan before building)

Decided 2026-11-21:
- **Order:** after Layer 4 and the eval, so the core "verified AI output" story is finished first.
- **Real receipts:** the splitter works fully with **manually entered receipts**, which are still verified by code. AI
  extraction is used only when a no-training provider is configured (paid Gemini or Claude). The free-tier policy is unchanged.
- **Upload page:** local mode only. Upload a photo, see the redacted preview, confirm, then it's extracted and verified.
  The demo stays read-only.

Sketch (to be detailed and approved):
- Per item: one person, equal split among several, or custom split (shares, percent or exact amounts; exact amounts must
  sum to the item's price).
- Tax, fees and receipt-level coupons are allocated in proportion to each person's items. Item discounts stay with their item.
- Rounding uses the largest-remainder method. **Invariant checked in code: shares sum exactly to the printed total.**
  Receipts still `needs_review` can't be split.
- Groups contain several receipts, each with a payer. The app nets balances and suggests the minimum set of payments to settle up.
- Names of real people are private data: they stay in the local database and never go to any model.

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

**Currency (decided 2026-11-21): the app accepts US and Canadian dollars only.** Any other currency is flagged (V9)
and kept out of prices. USD and CAD are never compared or mixed. The extraction prompt (extract-v2) tells CAD from USD
by GST/HST/PST lines, provinces and postal codes. **CORD stays as an eval-only benchmark:** the eval calls the extractor
directly and never stores those (rupiah) receipts.

### Eval run plan (2026-11-21, waiting for approval)

| Set | Receipts | Fields scored | Source of truth | API calls |
|---|---|---|---|---|
| **CORD-v2 sample** | 30 (seeded: 15 validation + 15 test), downloaded | item name (similarity ≥ 0.8), count, price, subtotal (18 labeled), total | CORD labels, normalized by `evals/cord.py` (tested) | **30 extraction calls** on `gemini-3.6-flash`, run as `--budget 15` per day: **2 days** |
| **Synthetic** | 21 extracted live (+1 pending) | store, date, subtotal/tax/total, every line price, flag correctness (misprints must be flagged), categories, base names, pack sizes | generator answer keys | **0**: the saved live extractions and labels are reused |
| **Redaction recall** | 22 synthetic images | card digits, auth code, member number | answer keys list each sensitive value | **0**: local OCR only |
| **Label corrections** | every saved human correction | does the current prompt now get it right? | `label_corrections.jsonl` | 1 categorize call |

- **Predictions are saved** (`evals/predictions/<set>/<id>.json`, model output only), so scoring can be re-run for free and
  the README numbers are reproducible. CORD predictions are committed with attribution. CORD images never are.
- **Redaction recall** is measured as: OCR the *original* image to confirm each sensitive value is readable, then OCR the
  *redacted* image and count how many are still readable. Counted only where the original was readable.
- **Report** (`evals/RESULTS.md` + a README table): per-field accuracy as `correct / scored` with a 95% Wilson interval
  (small samples get honest error bars), the flag rate, confidence-bucket accuracy for categories, and **every failure
  listed**. CORD and synthetic results are reported separately, because synthetic receipts are easier than photos.
- **What CORD can't tell us:** store and date (no labels), US formats, real crumpled-photo conditions on our own receipts.

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
  - **Sample (decided 2026-11-21):** a fixed, seeded sample of **30** receipts (15 validation + 15 test), because the
    free tier allows 20 requests/day. The run is resumable over two days. The README reports the sample size and confidence ranges.
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

---

## 8. Future directions (not in scope for Layers 1–4)

Ideas captured so they don't grow the current build. None of this is built.

### 8.1 More price sources
- Treat receipts as the primary, verified source.
- Possible extra sources behind a `PriceSource` interface (like the extractor interface): flyer data, public datasets
  (e.g. Project Hammer), StatCan averages.
- Record the source of every price; flag receipt prices far from flyer prices for the same week (cross-source verification).
- Avoid scraping store websites for the public project (terms of service, fragility).

### 8.2 "Strava for groceries"
- Personal records: lowest price ever paid for an item, best price per kg.
- Basket segments: a standard basket (milk, eggs, bread, bananas) with a weekly leaderboard inside a private club;
  the useful output is the store.
- Activity feed: each receipt becomes a post with savings and PRs; kudos.
- Clubs: roommates, neighbourhood, campus — shared price map.
- Challenges and badges and a year-in-review (spend, savings, personal inflation vs CPI).
- Guardrails: rank on unit price and deal-finding, never on lowest total food spend; no "you overpaid" shaming;
  private by default.
- Cold start: begin with small clubs; seed the map with flyer data.

### 8.3 Food posts ("what I made")
- Post a dish: photo, name, short recipe.
- Ingredients suggested from recent receipts and linked with one tap.
- Automatic cost per serving from receipt data.
- "Make this" button: shopping list priced at the viewer's nearby stores.
- Item recommendations attached to dishes or products, backed by verified purchases ("bought 12×") to resist spam.
- Mechanics: under-$5-per-serving challenges, remixes, pantry match, "what people are making with this week's deals".

### 8.4 Sharing controls for food posts
Per item, the poster chooses what to show:

| Field | Options |
|---|---|
| Store | show chain / hide |
| Price | exact / range ("under $3") / hide |
| Times bought | exact ("7×") / loose ("bought often") / hide |

- User-level defaults, with per-post overrides.
- Prices always shown with the date paid ("$2.49, Oct 2026").
- Separate opt-in for contributing hidden prices to anonymous area averages.
- Never expose the full receipt or the specific store location.
- Data model: purchase count = count of line items for a normalized product; sharing = `user_share_defaults` table +
  `post_item_share_overrides` table.

### 8.5 Suggested staging
1. Finish PriceLens Layers 1–4 (the engine).
2. Single-user "dish" page: link items, show cost per serving.
3. Small private friends feed with "make this" lists and sharing controls.
4. Clubs, basket leaderboards, year-in-review.
