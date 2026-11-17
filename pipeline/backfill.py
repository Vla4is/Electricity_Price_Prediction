"""Backfill Lithuanian day-ahead prices from the Elering NPS API into prices_raw.

Run from the project root:

    .venv/bin/python -m pipeline.backfill

What the API returns (checked 2026-11-17):
``{"success": true, "data": {"ee": [...], "fi": [...], "lv": [...], "lt": [...]}}``.
Each entry is ``{"timestamp": <UTC unix seconds>, "price": <float>}`` and marks
the START of a 15-minute slot. The API gives no unit; we assume EUR/MWh.
Both ``start`` and ``end`` are inclusive, and a day that hasn't been published
yet comes back as an empty list with HTTP 200.

The range is the 365 days that end at the end of tomorrow, Europe/Vilnius
time, so day-ahead prices for tomorrow are fetched once they are published.
Until then, tomorrow's slots are simply missing, which is not an error. One
request covers the whole range (a full year is about 35k entries, ~6 MB,
~1 s). The job is resumable: if prices_raw already has rows, it starts at
MAX(ts_utc) + 15 minutes.
"""

import json
import logging
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

from app.db import get_conn, init_db

API_URL = "https://dashboard.elering.ee/api/nps/price"
AREA = "lt"
LOCAL_TZ = ZoneInfo("Europe/Vilnius")
SLOT = timedelta(minutes=15)
LOOKBACK = timedelta(days=365)
HTTP_TIMEOUT_S = 30
RETRIES = 3            # retries after the first attempt
BACKOFF_BASE_S = 2     # waits 2 s, 4 s, 8 s between attempts
TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

log = logging.getLogger("backfill")


def end_of_tomorrow_utc(now: datetime) -> datetime:
    """Return midnight after tomorrow, Europe/Vilnius time, as a UTC datetime.

    For example, at any moment on 2026-11-17 in Vilnius (UTC+2) the result is
    2026-11-18T22:00:00Z. zoneinfo applies the DST offset in effect on that date.
    """
    day_after_tomorrow = now.astimezone(LOCAL_TZ).date() + timedelta(days=2)
    return datetime.combine(day_after_tomorrow, dtime(0), tzinfo=LOCAL_TZ).astimezone(UTC)


def to_ts_utc(dt: datetime) -> str:
    """Format a UTC datetime as ``YYYY-MM-DDTHH:MM:SSZ``."""
    return dt.astimezone(UTC).strftime(TS_FMT)


def parse_ts_utc(s: str) -> datetime:
    """Parse a ``YYYY-MM-DDTHH:MM:SSZ`` string into an aware UTC datetime."""
    return datetime.strptime(s, TS_FMT).replace(tzinfo=UTC)


def resolve_range(conn, now: datetime) -> tuple[datetime, datetime]:
    """Return the ``[start, end)`` slot range to fetch.

    ``end`` (exclusive) is the end of tomorrow in Europe/Vilnius, converted
    to UTC. ``start`` is ``end - 365 days`` (35,040 slots), or MAX(ts_utc) +
    15 minutes when there is already data, whichever is later. Requests
    longer than about one year return HTTP 400: 365 d + 1 s was accepted,
    365 d + 1 min was rejected (tested 2026-11-17).
    """
    end = end_of_tomorrow_utc(now)
    start = end - LOOKBACK
    last = conn.execute("SELECT MAX(ts_utc) FROM prices_raw").fetchone()[0]
    if last is not None:
        start = max(start, parse_ts_utc(last) + SLOT)
    return start, end


def fetch_prices(start: datetime, end: datetime) -> list[dict]:
    """Fetch the ``lt`` entries for ``[start, end)`` in one request.

    The API's ``end`` is inclusive, so we send ``end - 1 s``. That keeps out
    the entry that starts exactly at ``end``.
    Network errors, HTTP 429 and HTTP 5xx are retried ``RETRIES`` times with
    exponential backoff. Other HTTP errors are raised straight away.
    An empty list means "no data published" and is returned as ``[]``.
    """
    query = urllib.parse.urlencode({
        "start": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        "end": (end - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
    })
    url = f"{API_URL}?{query}"
    req = urllib.request.Request(url, headers={"User-Agent": "elprice-backfill/1.0"})

    for attempt in range(RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                body = json.load(resp)
            break
        except urllib.error.HTTPError as e:
            if e.code != 429 and e.code < 500:
                raise
            err = e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            err = e
        if attempt == RETRIES:
            raise RuntimeError(f"giving up after {RETRIES + 1} attempts: {err}") from err
        wait = BACKOFF_BASE_S * 2 ** attempt
        log.warning("attempt %d failed (%s), retrying in %ds", attempt + 1, err, wait)
        time.sleep(wait)

    if not body.get("success"):
        raise RuntimeError(f"API returned success=false: {str(body)[:200]}")
    return body.get("data", {}).get(AREA) or []


def to_rows(entries: list[dict], start: datetime, end: datetime, fetched_at: str) -> list[tuple]:
    """Turn API entries into prices_raw rows and keep only those in ``[start, end)``.

    The range filter is a guard. A timestamp far in the future would make the
    prune_old_prices trigger delete valid history.
    """
    lo, hi = start.timestamp(), end.timestamp()
    rows = []
    for e in entries:
        ts, price = e.get("timestamp"), e.get("price")
        if ts is None or price is None or not (lo <= ts < hi):
            continue
        rows.append((to_ts_utc(datetime.fromtimestamp(ts, UTC)), float(price), fetched_at))
    return rows


def log_quality(conn, result: str, details: dict) -> None:
    """Write one 'backfill' row to quality_log."""
    with conn:
        conn.execute(
            "INSERT INTO quality_log (checked_at, check_name, result, details) "
            "VALUES (?, 'backfill', ?, ?)",
            (to_ts_utc(datetime.now(UTC)), result, json.dumps(details)),
        )


def backfill(now: datetime | None = None) -> int:
    """Fetch missing prices, insert them and log the outcome. Returns rows inserted.

    Inserts use INSERT OR IGNORE in a single transaction. Slots that are not
    published yet (normally tomorrow's, before the day-ahead auction) are just
    missing from the response. The quality_log result is:
    - 'ok': rows were inserted; or there was nothing to fetch; or the range
      starts in the future and nothing is published for it yet
    - 'warn': the API returned no data although the range includes past
      slots, which should exist
    - 'failed': an error happened; it is logged and then re-raised
    """
    now = now or datetime.now(UTC)
    init_db()
    conn = get_conn()
    try:
        start, end = resolve_range(conn, now)
        details = {"start": to_ts_utc(start), "end_exclusive": to_ts_utc(end)}
        if start >= end:
            log_quality(conn, "ok", {**details, "rows_inserted": 0, "note": "up to date"})
            log.info("already up to date (%s)", details)
            return 0

        try:
            entries = fetch_prices(start, end)
        except Exception as e:
            log_quality(conn, "failed", {**details, "rows_inserted": 0, "error": str(e)})
            raise

        rows = to_rows(entries, start, end, to_ts_utc(now))
        with conn:
            cur = conn.executemany("INSERT OR IGNORE INTO prices_raw VALUES (?, ?, ?)", rows)
            inserted = max(cur.rowcount, 0)  # sqlite3_changes(): excludes trigger deletes

        if rows:
            result, note = "ok", {}
        elif start > now:
            result, note = "ok", {"note": "not published yet"}
        else:
            result, note = "warn", {"note": "no data"}
        log_quality(conn, result, {**details, "rows_received": len(entries),
                                   "rows_inserted": inserted, **note})
        log.info("inserted %d rows for %s", inserted, details)
        return inserted
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    try:
        backfill()
    except Exception as exc:
        log.error("backfill failed: %s", exc)
        sys.exit(1)
