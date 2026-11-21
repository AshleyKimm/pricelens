-- Layer 2: categorization cache + data source tracking.

-- Where the receipt image came from. Existing rows default to 'private': if we don't know,
-- assume it's personal data (the safe default for the provider policy).
ALTER TABLE receipts ADD COLUMN source TEXT NOT NULL DEFAULT 'private'
  CHECK (source IN ('public', 'private'));

-- Printed item name, normalized for cache lookups (uppercase, single spaces). Backfilled in code.
ALTER TABLE line_items ADD COLUMN description_key TEXT;
CREATE INDEX IF NOT EXISTS idx_line_items_desc_key ON line_items(description_key);

-- One label per (store, printed name). AI rows are a cache; human rows are corrections and are
-- never overwritten by AI.
CREATE TABLE IF NOT EXISTS item_labels (
  id              INTEGER PRIMARY KEY,
  store_id        INTEGER NOT NULL REFERENCES stores(id),
  description_key TEXT NOT NULL,
  description     TEXT NOT NULL,             -- printed name as first seen
  normalized_name TEXT,                      -- base product, compared across stores ('rice')
  variety         TEXT,                      -- what distinguishes it ('jasmine'), NULL if nothing
  category        TEXT NOT NULL CHECK (category IN (
                    'produce','meat_seafood','dairy_eggs','bakery','deli_prepared','frozen','pantry',
                    'snacks_sweets','beverages','alcohol','household','personal_care','health','baby_pet',
                    'restaurant_food','other','uncertain')),
  confidence      TEXT CHECK (confidence IN ('high','medium','low')),
  needs_review    INTEGER NOT NULL DEFAULT 0,
  review_reasons  TEXT NOT NULL DEFAULT '[]',
  source          TEXT NOT NULL CHECK (source IN ('ai','human')),
  model           TEXT,
  prompt_version  TEXT,
  created_at      TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at      TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE (store_id, description_key)
);
CREATE INDEX IF NOT EXISTS idx_item_labels_name ON item_labels(normalized_name);
