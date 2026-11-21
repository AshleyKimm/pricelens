-- PriceLens Layer 1 schema. Money is always INTEGER cents.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS stores (
  id        INTEGER PRIMARY KEY,
  name      TEXT NOT NULL,              -- display name as first seen
  name_key  TEXT NOT NULL UNIQUE        -- lowercased, collapsed whitespace, for matching
);

CREATE TABLE IF NOT EXISTS receipts (
  id              INTEGER PRIMARY KEY,
  image_sha256    TEXT NOT NULL UNIQUE,     -- duplicate guard (hash of the ORIGINAL file)
  redacted_path   TEXT,                     -- local copy of exactly what was sent to the API
  store_id        INTEGER REFERENCES stores(id),
  store_name_raw  TEXT,
  purchase_date   TEXT,                     -- ISO 'YYYY-MM-DD', NULL if unreadable
  date_raw        TEXT,
  currency        TEXT NOT NULL DEFAULT 'USD',
  subtotal_cents  INTEGER,                  -- printed value; NULL if not printed
  tax_cents       INTEGER,                  -- sum of printed tax lines
  total_cents     INTEGER,
  legibility      TEXT CHECK (legibility IN ('good','partial','poor')),
  status          TEXT NOT NULL CHECK (status IN ('verified','needs_review','rejected')),
  review_reasons  TEXT NOT NULL DEFAULT '[]',  -- JSON [{code, message, blocking}]
  model           TEXT NOT NULL,            -- model that actually served the request
  prompt_version  TEXT NOT NULL,
  extraction_json TEXT NOT NULL,            -- full model output, for audit and re-verification
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  reviewed_at     TEXT                      -- set when a human approves/rejects/corrects
);

CREATE TABLE IF NOT EXISTS line_items (
  id               INTEGER PRIMARY KEY,
  receipt_id       INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
  line_no          INTEGER NOT NULL,        -- 1-based order on the receipt
  raw_text         TEXT NOT NULL,           -- exact printed line(s)
  description      TEXT,
  kind             TEXT NOT NULL CHECK (kind IN ('item','discount','coupon','refund','fee')),
  section          TEXT NOT NULL CHECK (section IN ('items','after_subtotal')),
  quantity         REAL,                    -- 2, or 1.32 for weighed items
  unit             TEXT CHECK (unit IN ('each','lb','kg','oz','g')),
  unit_price_cents INTEGER,
  line_total_cents INTEGER,                 -- NULL only if unreadable (receipt is then flagged)
  applies_to_line  INTEGER,                 -- line_no of the item a discount belongs to
  UNIQUE (receipt_id, line_no)
);

CREATE INDEX IF NOT EXISTS idx_receipts_store_date ON receipts(store_id, purchase_date);
CREATE INDEX IF NOT EXISTS idx_line_items_desc     ON line_items(description);
