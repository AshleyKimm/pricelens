-- Layer 4: batch runs and their failures, so a run can be resumed with `retry-failed`.
CREATE TABLE IF NOT EXISTS ingest_runs (
  id            INTEGER PRIMARY KEY,
  started_at    TEXT NOT NULL DEFAULT (datetime('now')),
  finished_at   TEXT,
  source        TEXT NOT NULL,            -- folder, or 'retry-failed of run N'
  workers       INTEGER NOT NULL,
  rpm           REAL,
  budget        INTEGER,
  requests_sent INTEGER NOT NULL DEFAULT 0,
  stopped       TEXT,                     -- why the run stopped early (daily quota, circuit open...), NULL if it finished
  summary       TEXT                      -- JSON counts
);

CREATE TABLE IF NOT EXISTS ingest_failures (
  id          INTEGER PRIMARY KEY,
  run_id      INTEGER NOT NULL REFERENCES ingest_runs(id) ON DELETE CASCADE,
  image_path  TEXT NOT NULL,
  sha256      TEXT,
  error_type  TEXT NOT NULL,             -- e.g. TransientAPIError, UnreadableImageError, NotAttempted
  message     TEXT NOT NULL,
  attempts    INTEGER NOT NULL DEFAULT 0,
  retryable   INTEGER NOT NULL,           -- 1 = worth trying again later
  resolved_at TEXT                        -- set when a later run stores it
);
CREATE INDEX IF NOT EXISTS idx_ingest_failures_open ON ingest_failures(resolved_at, retryable);
