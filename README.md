# LT Electricity Price Forecast

Fetches Lithuanian day-ahead electricity prices from the Elering API into SQLite
and forecasts the next 144 hours with Chronos-Bolt on CPU. A FastAPI dashboard
shows the real prices and the forecast.

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env                      # set API_KEY
.venv/bin/python scripts/download_model.py
.venv/bin/python -m pipeline.backfill

sudo cp elprice.service elproject-daily.service elproject-daily.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now elprice elproject-daily.timer
```

The dashboard is served at http://127.0.0.1:8000.

## Commands

| Command | What it does |
|---|---|
| `.venv/bin/python scripts/download_model.py` | Downloads `MODEL_NAME` into `models/` |
| `.venv/bin/python -m pipeline.backfill` | Loads the last year of prices; later runs only fetch new data |
| `.venv/bin/python -m pipeline.daily_job` | Fetches prices, repairs gaps, makes the forecast (the timer runs this at 15:00, 16:00 and 17:00 Vilnius time) |
| `.venv/bin/python -m pipeline.backtest [--days 21]` | Compares the model with a naive baseline; writes `backtest_results.json` |
