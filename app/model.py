"""Chronos-Bolt price forecaster, loaded once at startup and kept in app.state.

Notes on chronos-forecasting 2.3.2 (checked against the installed library):
- ``BaseChronosPipeline.from_pretrained("amazon/chronos-bolt-small")`` returns
  a ``ChronosBoltPipeline``. Its ``model_context_length`` is 2048 and its
  native ``model_prediction_length`` is 64.
- ``predict_quantiles(inputs, prediction_length, quantile_levels)`` returns
  ``(quantiles, mean)``. ``quantiles`` has shape (batch, horizon, n_levels),
  in fp32 on the CPU. Bolt is trained on the levels 0.1–0.9, so p10/p50/p90
  are read directly and not interpolated.
- A ``prediction_length`` above 64 is produced step by step, each step using
  the previous one's output, and the library emits a UserWarning that quality
  may degrade. We log that warning on each request.
"""

import logging
import threading
import time
import warnings
from dataclasses import dataclass

import app.config  # noqa: F401  loads .env (HF_HOME) before huggingface/chronos are imported
import torch
from chronos import BaseChronosPipeline
from huggingface_hub import snapshot_download
from huggingface_hub.errors import LocalEntryNotFoundError

log = logging.getLogger("uvicorn.error")

QUANTILE_LEVELS = [0.1, 0.5, 0.9]


@dataclass
class Forecast:
    """Quantile forecast for one series, with lists of length ``horizon``."""

    p10: list[float]
    p50: list[float]
    p90: list[float]
    elapsed_ms: float
    warnings: list[str]


class Forecaster:
    """Thin wrapper around a Chronos pipeline running on the CPU.

    Predictions are serialised with a lock. The VM has 2 cores and about 4 GB
    of RAM, so running predictions in parallel would only compete for them.
    """

    def __init__(self, model_name: str):
        """Load ``model_name`` onto the CPU, preferring the local snapshot in HF_HOME.

        If the model is already cached (``scripts/download_model.py``), it is
        loaded from that path and nothing is fetched over the network.
        Otherwise it falls back to downloading from the Hub.
        """
        self.model_name = model_name
        try:
            self.source = snapshot_download(model_name, local_files_only=True)
        except LocalEntryNotFoundError:
            log.warning("%s not in local cache; downloading from the Hub", model_name)
            self.source = model_name
        self.pipeline = BaseChronosPipeline.from_pretrained(
            self.source, device_map="cpu", dtype=torch.float32
        )
        self.pipeline.model.eval()
        self.max_context = self.pipeline.model_context_length
        self.native_horizon = self.pipeline.model_prediction_length
        self._lock = threading.Lock()

    def predict(self, prices: list[float], horizon: int) -> Forecast:
        """Forecast ``horizon`` steps after ``prices`` (ordered oldest to newest).

        Context longer than the model's ``model_context_length`` is truncated
        to the newest values by the library.
        """
        context = torch.tensor(prices, dtype=torch.float32)
        with self._lock, warnings.catch_warnings(record=True) as caught, torch.inference_mode():
            warnings.simplefilter("always")
            t0 = time.perf_counter()
            quantiles, _mean = self.pipeline.predict_quantiles(
                context, prediction_length=horizon, quantile_levels=QUANTILE_LEVELS
            )
            elapsed_ms = (time.perf_counter() - t0) * 1000
        msgs = [str(w.message) for w in caught]
        for m in msgs:
            log.warning("chronos (horizon=%d): %s", horizon, m)
        q = quantiles[0]  # (horizon, 3)
        return Forecast(
            p10=q[:, 0].tolist(),
            p50=q[:, 1].tolist(),
            p90=q[:, 2].tolist(),
            elapsed_ms=round(elapsed_ms, 1),
            warnings=msgs,
        )


def load_forecaster(model_name: str) -> Forecaster | None:
    """Load the forecaster, or log the error and return None so the app still starts."""
    t0 = time.perf_counter()
    try:
        f = Forecaster(model_name)
    except Exception:
        log.exception("failed to load model %s", model_name)
        return None
    log.info(
        "loaded %s from %s in %.1fs (context<=%d, native horizon=%d)",
        model_name, f.source, time.perf_counter() - t0, f.max_context, f.native_horizon,
    )
    return f
