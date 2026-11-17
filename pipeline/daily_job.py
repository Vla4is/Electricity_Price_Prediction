"""Daily live forecast job: fetch new prices, repair gaps, forecast, prune old runs.

Run from the project root (the systemd timer runs it at 15:00, 16:00 and 17:00 Vilnius time):

    .venv/bin/python -m pipeline.daily_job

Each step writes one row to quality_log. check_name is one of: fetch,
gap_repair, model_input, forecast, retention.
"""

import json
import logging
import sys
import urllib.request
from datetime import UTC, datetime, timedelta

from app.config import settings
from app.db import get_conn, init_db
from pipeline.backfill import fetch_prices, parse_ts_utc, resolve_range, to_rows, to_ts_utc

FORECAST_URL = "http://127.0.0.1:8000/forecast"
HOUR = timedelta(hours=1)
REPAIR_TRIES = 3
MAX_FILLED_HOURS = 2
RETENTION = timedelta(days=30)

log = logging.getLogger("daily_job")


def log_quality(conn, check_name: str, result: str, details: dict) -> None:
    """Write one row to quality_log, and echo it to the job log."""
    log.info("%s %s %s", check_name, result, details)
    with conn:
        conn.execute(
            "INSERT INTO quality_log (checked_at, check_name, result, details) VALUES (?, ?, ?, ?)",
            (to_ts_utc(datetime.now(UTC)), check_name, result, json.dumps(details)),
        )


def insert_prices(conn, start: datetime, end: datetime, now: datetime) -> int:
    """Fetch ``[start, end)`` from Elering and INSERT OR IGNORE it. Returns rows inserted."""
    rows = to_rows(fetch_prices(start, end), start, end, to_ts_utc(now))
    with conn:
        cur = conn.executemany("INSERT OR IGNORE INTO prices_raw VALUES (?, ?, ?)", rows)
    return max(cur.rowcount, 0)


def window_hours(cutoff: datetime) -> list[datetime]:
    """The CONTEXT_HOURS hour starts ending at (and including) ``cutoff``, oldest first."""
    n = settings.context_hours
    return [cutoff - (n - 1 - i) * HOUR for i in range(n)]


def hourly_prices(conn, hours: list[datetime]) -> dict[str, float]:
    """Complete-hour prices from prices_hourly for the span of ``hours``, keyed by ts_utc."""
    rows = conn.execute(
        "SELECT ts_utc, price_eur_mwh FROM prices_hourly WHERE ts_utc BETWEEN ? AND ?",
        (to_ts_utc(hours[0]), to_ts_utc(hours[-1])),
    )
    return {r[0]: r[1] for r in rows}


def missing_hours(conn, hours: list[datetime]) -> list[datetime]:
    """Hours in ``hours`` that are missing from prices_hourly (absent or incomplete)."""
    have = hourly_prices(conn, hours)
    return [h for h in hours if to_ts_utc(h) not in have]


def group_ranges(hours: list[datetime]) -> list[tuple[datetime, datetime]]:
    """Group sorted hours into ``[start, end)`` ranges of neighbouring hours."""
    ranges: list[list[datetime]] = []
    for h in hours:
        if ranges and ranges[-1][1] == h:
            ranges[-1][1] = h + HOUR
        else:
            ranges.append([h, h + HOUR])
    return [(a, b) for a, b in ranges]


def repair_gaps(conn, hours: list[datetime], now: datetime) -> None:
    """Re-fetch each range of missing hours (up to REPAIR_TRIES times) and log the result."""
    before = missing_hours(conn, hours)
    ranges = group_ranges(before)
    for start, end in ranges:
        for _ in range(REPAIR_TRIES):
            try:
                insert_prices(conn, start, end, now)
            except Exception as e:
                log.warning("gap re-fetch %s..%s failed: %s", start, end, e)
            if not missing_hours(conn, [h for h in hours if start <= h < end]):
                break
    after = missing_hours(conn, hours)
    log_quality(conn, "gap_repair", "ok" if not after else "warn", {
        "missing_before": len(before), "missing_after": len(after),
        "ranges": [[to_ts_utc(a), to_ts_utc(b)] for a, b in ranges],
    })


def call_forecast(prices: list[float]) -> dict:
    """POST the context prices to the local /forecast endpoint and return the JSON response."""
    body = json.dumps({"prices": prices, "horizon": settings.horizon_hours}).encode()
    req = urllib.request.Request(FORECAST_URL, data=body, method="POST", headers={
        "Content-Type": "application/json", "X-API-Key": settings.api_key,
    })
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.load(resp)


def run(now: datetime | None = None) -> bool:
    """Run every step in order. Returns False if the job failed."""
    now = now or datetime.now(UTC)
    init_db()
    conn = get_conn()
    try:
        # 1. Fetch new prices (up to the end of tomorrow, Vilnius time).
        start, end = resolve_range(conn, now)
        try:
            n = insert_prices(conn, start, end, now) if start < end else 0
            log_quality(conn, "fetch", "ok", {"start": to_ts_utc(start),
                                              "end_exclusive": to_ts_utc(end), "rows_inserted": n})
        except Exception as e:
            log_quality(conn, "fetch", "warn", {"error": str(e)})  # still forecast from what we have

        # 2. Cutoff = newest complete hour.
        last = conn.execute("SELECT MAX(ts_utc) FROM prices_hourly").fetchone()[0]
        if last is None:
            log_quality(conn, "model_input", "failed", {"error": "prices_hourly is empty"})
            return False
        cutoff = parse_ts_utc(last)
        hours = window_hours(cutoff)

        # 3. Gap repair.
        repair_gaps(conn, hours, now)

        # 4. Model input: fill at most MAX_FILLED_HOURS from the previous hour (in memory only).
        have = hourly_prices(conn, hours)
        prices = [have.get(to_ts_utc(h)) for h in hours]
        filled = [to_ts_utc(h) for h, p in zip(hours, prices) if p is None]
        if len(filled) > MAX_FILLED_HOURS:
            log_quality(conn, "model_input", "failed",
                        {"cutoff": last, "missing": len(filled), "first_missing": filled[:10]})
            return False
        if prices[0] is None:  # no previous hour inside the window
            prices[0] = next(p for p in prices if p is not None)
        for i in range(1, len(prices)):
            if prices[i] is None:
                prices[i] = prices[i - 1]
        if filled:
            log_quality(conn, "model_input", "warn", {"cutoff": last, "filled_hours": filled})

        # 5. Idempotency.
        done = conn.execute(
            "SELECT run_id FROM forecast_runs WHERE kind='live' AND status='ok' AND cutoff_ts_utc=?",
            (last,),
        ).fetchone()
        if done:
            log_quality(conn, "forecast", "ok",
                        {"note": "already forecast", "cutoff": last, "run_id": done[0]})
            return True

        # 6. Forecast.
        with conn:
            run_id = conn.execute(
                "INSERT INTO forecast_runs (made_at, model, kind, status, context_hours, "
                "horizon_hours, cutoff_ts_utc) VALUES (?, ?, 'live', 'running', ?, ?, ?)",
                (to_ts_utc(now), settings.model_name, settings.context_hours,
                 settings.horizon_hours, last),
            ).lastrowid
        try:
            r = call_forecast(prices)
            if len(r["p50"]) != settings.horizon_hours:
                raise ValueError(f"expected {settings.horizon_hours} values, got {len(r['p50'])}")
            rows = [(run_id, to_ts_utc(cutoff + (i + 1) * HOUR), p10, p50, p90)
                    for i, (p10, p50, p90) in enumerate(zip(r["p10"], r["p50"], r["p90"]))]
            with conn:
                conn.executemany("INSERT INTO forecasts VALUES (?, ?, ?, ?, ?)", rows)
                conn.execute("UPDATE forecast_runs SET status='ok' WHERE run_id=?", (run_id,))
            log_quality(conn, "forecast", "ok", {"run_id": run_id, "cutoff": last,
                                                 "rows": len(rows), "elapsed_ms": r["elapsed_ms"]})
        except Exception as e:
            with conn:
                conn.execute("UPDATE forecast_runs SET status='failed' WHERE run_id=?", (run_id,))
            log_quality(conn, "forecast", "failed", {"run_id": run_id, "cutoff": last, "error": str(e)})
            return False

        # 7. Retention: the cascade removes the old runs' forecast rows.
        with conn:
            deleted = conn.execute("DELETE FROM forecast_runs WHERE made_at < ?",
                                   (to_ts_utc(now - RETENTION),)).rowcount
        log_quality(conn, "retention", "ok", {"runs_deleted": deleted})
        return True
    finally:
        conn.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(0 if run() else 1)
