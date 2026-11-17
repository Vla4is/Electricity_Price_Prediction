# elproject — notes for Claude

The README is written by the user. Don't create or edit it unless asked.
Don't commit; the user commits.

## Setup (from /home/azureuser/elproject, Ubuntu, Python 3.12)
```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt     # file has --extra-index-url for CPU torch (torch==…+cpu)
cp .env.example .env && chmod 600 .env        # set a real API_KEY (secrets.token_urlsafe(32))
.venv/bin/python scripts/download_model.py    # into $HF_HOME=models/ (~183 MB); skips files already present
.venv/bin/python -m pipeline.backfill         # 1 year of prices → elprice.db; resumes from MAX(ts_utc)
sudo cp elprice.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now elprice
curl -s 127.0.0.1:8000/health                 # {"status":"ok","model_loaded":true}
```
- The unit in `/etc/systemd/system/elprice.service` is a COPY of the repo file, not a symlink. Re-copy and daemon-reload after editing it.
- The unit has `EnvironmentFile=.env`, so HF_HOME is set before Python starts. `app/model.py` also imports `app.config` (load_dotenv) before chronos.
- Cloudflare Tunnel: `cloudflared/config.yml` maps catchingfish.online to localhost:8000. The credentials JSON is ignored by git. No cloudflared systemd service is installed yet (as of 2026-11-17).
- Ignored by git: `.env`, `*.db*`, `models/`, `cloudflared/`. Never print `API_KEY`; redact it with sed when showing `.env`.

## Layout
- `app/db.py`: SQLite schema and `init_db()` (idempotent, runs at startup).
- `app/config.py`: settings from .env.
- `app/model.py`: Chronos forecaster, kept in `app.state.forecaster`.
- `app/main.py`: routes `/`, `/health`, `POST /forecast` (X-API-Key; no DB access).
- `pipeline/backfill.py`: Elering → `prices_raw`.
- `scripts/download_model.py`: deliberately minimal, about 6 lines. Keep it that way.

## Facts verified (2026-11-17)
- Elering `GET /api/nps/price?start=…&end=…` (ISO with Z) returns `data.lt` as a list of `{timestamp: unix s UTC, price}`. Each entry is the START of a 15-minute slot. `end` is inclusive. Unpublished days come back as `[]` with HTTP 200. No unit in the response (assumed EUR/MWh). Ranges over ~365 days return HTTP 400 (365 d + 1 s OK, 365 d + 1 min rejected).
- Backfill end = end of tomorrow in Europe/Vilnius, converted to UTC.
- chronos-forecasting 2.3.2 with amazon/chronos-bolt-small: context ≤ 2048, native horizon 64 (longer runs autoregressively and emits a UserWarning), trained on quantiles 0.1–0.9. Inference for 672 → 144 takes ~650 ms on 2 vCPU. Service RSS is ~600 MiB on a 3.8 GiB VM with no swap.
- Daily forecast: cutoff = newest `prices_hourly` hour, 672 h of context, HORIZON_HOURS = 144 (not 168). `/forecast` accepts 1–168.
