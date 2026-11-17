"""SQLite storage for electricity prices, forecasts and data-quality checks.

All timestamps are stored as UTC text in the form ``YYYY-MM-DDTHH:MM:SSZ``.
That format sorts lexicographically in time order, and SQLite's date
functions accept it directly.
"""

import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "elprice.db"

SCHEMA = """
-- Raw day-ahead prices from the Elering NPS API, one row per 15-minute slot.
-- ts_utc is the START of the slot (YYYY-MM-DDTHH:MM:SSZ, UTC).
-- price_eur_mwh: the API returns no unit. EUR/MWh (excl. VAT) is an
-- assumption based on Elering's published NPS prices and the value range seen.
CREATE TABLE IF NOT EXISTS prices_raw (
    ts_utc        TEXT PRIMARY KEY,
    price_eur_mwh REAL NOT NULL,
    fetched_at    TEXT NOT NULL
);

-- Hourly averages. Only complete hours (all four 15-minute slots) are included.
CREATE VIEW IF NOT EXISTS prices_hourly AS
SELECT substr(ts_utc, 1, 13) || ':00:00Z' AS ts_utc,
       AVG(price_eur_mwh)                 AS price_eur_mwh,
       COUNT(*)                           AS slots
FROM prices_raw
GROUP BY substr(ts_utc, 1, 13)
HAVING COUNT(*) = 4;

-- One row per forecast run (live or backtest).
CREATE TABLE IF NOT EXISTS forecast_runs (
    run_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    made_at       TEXT,
    model         TEXT,
    kind          TEXT CHECK (kind IN ('live', 'backtest')),
    status        TEXT CHECK (status IN ('running', 'ok', 'failed')),
    context_hours INT,
    horizon_hours INT,
    cutoff_ts_utc TEXT
);

-- Quantile forecasts for each target hour of a run.
CREATE TABLE IF NOT EXISTS forecasts (
    run_id        INT REFERENCES forecast_runs(run_id) ON DELETE CASCADE,
    target_ts_utc TEXT,
    p10           REAL,
    p50           REAL,
    p90           REAL,
    PRIMARY KEY (run_id, target_ts_utc)
);

CREATE INDEX IF NOT EXISTS idx_forecasts_run_id ON forecasts(run_id);

-- Results of data-quality checks.
CREATE TABLE IF NOT EXISTS quality_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    checked_at TEXT,
    check_name TEXT,
    result     TEXT CHECK (result IN ('ok', 'warn', 'failed')),
    details    TEXT
);

-- Forecast rows of the newest successful live run.
-- "Newest" means the latest made_at; run_id breaks ties.
CREATE VIEW IF NOT EXISTS latest_forecast AS
SELECT f.run_id, f.target_ts_utc, f.p10, f.p50, f.p90,
       r.made_at, r.model, r.cutoff_ts_utc
FROM forecasts f
JOIN forecast_runs r ON r.run_id = f.run_id
WHERE f.run_id = (
    SELECT run_id FROM forecast_runs
    WHERE kind = 'live' AND status = 'ok'
    ORDER BY made_at DESC, run_id DESC
    LIMIT 1
);

-- Keep a rolling year of raw prices: each insert removes rows that are more
-- than 365 days older than the inserted row.
CREATE TRIGGER IF NOT EXISTS prune_old_prices
AFTER INSERT ON prices_raw
BEGIN
    DELETE FROM prices_raw
    WHERE ts_utc < strftime('%Y-%m-%dT%H:%M:%SZ', NEW.ts_utc, '-365 days');
END;
"""


def get_conn() -> sqlite3.Connection:
    """Open a connection to the project database.

    The connection can be shared across threads (``check_same_thread=False``)
    and returns rows as ``sqlite3.Row``. Every connection turns on WAL mode,
    a 5-second busy timeout and foreign-key enforcement.
    """
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    """Create all tables, views, indexes and triggers if they don't exist.

    It is safe to call this on every startup.
    """
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()
