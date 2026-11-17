"""Backtest the live forecast setup on recent days and compare it with a naive baseline.

    .venv/bin/python -m pipeline.backtest [--days 21]

For each day D among the last ``--days`` days (Vilnius), the cutoff is the
last hour of D+1. That matches the live job, which runs on D with prices
through the end of tomorrow. The forecast covers the next HORIZON_HOURS
hours, i.e. the six Vilnius days D+2..D+7. Cutoffs are skipped if any target
hour or any context hour is missing from prices_hourly. No data after the
cutoff is ever sent to the model.

Each run is stored as kind='backtest' and replaces an earlier backtest run
with the same cutoff. Results are in c/kWh: MAE of Chronos p50, MAE of the
naive "same hour 7 days earlier" baseline, and p10–p90 coverage. They are
printed and saved to backtest_results.json.
"""

import argparse
import json
from datetime import UTC, date, datetime, time as dtime, timedelta

from app.config import settings
from app.db import get_conn, init_db
from pipeline.backfill import LOCAL_TZ, to_ts_utc
from pipeline.daily_job import HOUR, call_forecast, window_hours

RESULTS_PATH = "backtest_results.json"


def cutoff_for(day: date) -> datetime:
    """Start of the last hour of ``day + 1`` in Europe/Vilnius, as UTC."""
    midnight = datetime.combine(day + timedelta(days=2), dtime(0), tzinfo=LOCAL_TZ)
    return midnight.astimezone(UTC) - HOUR


def store_run(conn, cutoff: str, made_at: str, rows: list[tuple]) -> None:
    """Replace any backtest run for ``cutoff`` with a new 'ok' run and its forecast rows."""
    with conn:
        conn.execute("DELETE FROM forecast_runs WHERE kind='backtest' AND cutoff_ts_utc=?", (cutoff,))
        run_id = conn.execute(
            "INSERT INTO forecast_runs (made_at, model, kind, status, context_hours, horizon_hours, "
            "cutoff_ts_utc) VALUES (?, ?, 'backtest', 'ok', ?, ?, ?)",
            (made_at, settings.model_name, settings.context_hours, settings.horizon_hours, cutoff),
        ).lastrowid
        conn.executemany("INSERT INTO forecasts VALUES (?, ?, ?, ?, ?)",
                         [(run_id, *r) for r in rows])


def main(days: int) -> None:
    """Run the backtest over the last ``days`` days, then print and save the summary."""
    init_db()
    conn = get_conn()
    prices = dict(conn.execute("SELECT ts_utc, price_eur_mwh FROM prices_hourly").fetchall())
    horizon, n_days = settings.horizon_hours, settings.horizon_hours // 24
    today = datetime.now(LOCAL_TZ).date()
    # per forecast day: lists of Chronos errors, naive errors, and in-band flags
    stats = {d: ([], [], []) for d in range(1, n_days + 1)}
    used = []

    for k in range(days, 0, -1):
        cutoff = cutoff_for(today - timedelta(days=k))
        cut = to_ts_utc(cutoff)
        targets = [cutoff + (i + 1) * HOUR for i in range(horizon)]
        context = window_hours(cutoff)
        missing = [to_ts_utc(t) for t in targets + context if to_ts_utc(t) not in prices]
        if missing:
            print(f"skip cutoff {cut}: {len(missing)} hours missing")
            continue

        r = call_forecast([prices[to_ts_utc(h)] for h in context])
        rows = list(zip(map(to_ts_utc, targets), r["p10"], r["p50"], r["p90"]))
        store_run(conn, cut, to_ts_utc(datetime.now(UTC)), rows)
        used.append(cut)

        for i, (ts, p10, p50, p90) in enumerate(rows):
            real = prices[ts]
            naive = prices[to_ts_utc(targets[i] - 168 * HOUR)]  # inside the context, before the cutoff
            chronos_err, naive_err, covered = stats[i // 24 + 1]
            chronos_err.append(abs(p50 - real) / 10)
            naive_err.append(abs(naive - real) / 10)
            covered.append(p10 <= real <= p90)
    conn.close()

    if not used:
        print("no usable cutoffs")
        return
    mean = lambda xs: sum(xs) / len(xs)
    groups = {**{str(d): s for d, s in stats.items()},
              "overall": tuple(sum((s[j] for s in stats.values()), []) for j in range(3))}
    table = [{"day": name, "chronos_mae": round(mean(c), 3), "naive_mae": round(mean(n), 3),
              "coverage_pct": round(100 * mean(cov), 1)} for name, (c, n, cov) in groups.items()]

    print(f"\n{len(used)} cutoffs: {used[0]} .. {used[-1]}   (MAE in c/kWh)")
    print(f"{'day':>8} {'Chronos MAE':>12} {'naive MAE':>10} {'coverage %':>11}")
    for t in table:
        print(f"{t['day']:>8} {t['chronos_mae']:>12.2f} {t['naive_mae']:>10.2f} {t['coverage_pct']:>11.1f}")

    with open(RESULTS_PATH, "w") as f:
        json.dump({"generated_at": to_ts_utc(datetime.now(UTC)), "model": settings.model_name,
                   "context_hours": settings.context_hours, "horizon_hours": horizon,
                   "cutoffs": used, "results": table}, f, indent=2)
    print(f"saved {RESULTS_PATH}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--days", type=int, default=21, help="number of past days D to test (default 21)")
    main(parser.parse_args().days)
