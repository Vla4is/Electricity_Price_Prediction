"""LT Electricity Price Forecast web application."""

import math
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, field_validator

from app.config import settings
from app.db import init_db
from app.model import load_forecaster

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create the database schema and load the forecasting model once on startup."""
    init_db()
    app.state.forecaster = load_forecaster(settings.model_name)
    yield
    app.state.forecaster = None


app = FastAPI(title="LT Electricity Price Forecast", lifespan=lifespan)
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """Return 422 with type/loc/msg only.

    FastAPI's default handler echoes the rejected input back. That fails with
    a 500 when the input contains NaN, which JSON can't encode, and it would
    repeat thousands of prices.
    """
    errors = [{k: e[k] for k in ("type", "loc", "msg") if k in e} for e in exc.errors()]
    return JSONResponse(status_code=422, content={"detail": errors})


def require_api_key(x_api_key: Annotated[str | None, Header()] = None) -> None:
    """Reject the request with 401 unless the X-API-Key header matches API_KEY.

    If API_KEY is empty, every request is rejected.
    """
    if not settings.api_key or not x_api_key or not secrets.compare_digest(
        x_api_key.encode(), settings.api_key.encode()
    ):
        raise HTTPException(status_code=401, detail="invalid or missing API key")


class ForecastRequest(BaseModel):
    """Hourly price history (oldest to newest) and the number of hours to forecast."""

    prices: list[Annotated[float, Field(allow_inf_nan=False)]] = Field(
        min_length=168, max_length=2048,
        description="Hourly prices in EUR/MWh, oldest first; no NaN, inf or null values.",
    )
    horizon: int = Field(
        default=settings.horizon_hours, ge=1, le=168,
        description="Hours to forecast (1–168). Defaults to HORIZON_HOURS from .env (144), "
                    "which the daily live job also uses.",
    )

    @field_validator("prices", mode="before")
    @classmethod
    def no_empty_values(cls, v):
        """Reject null or blank entries with a clear message instead of a type error."""
        if isinstance(v, list):
            for i, x in enumerate(v):
                if x is None or (isinstance(x, str) and not x.strip()):
                    raise ValueError(f"prices[{i}] is empty")
        return v


class ForecastResponse(BaseModel):
    """Quantile forecast; p10/p50/p90 each contain ``horizon`` values."""

    model: str
    context_length: int
    horizon: int
    p10: list[float]
    p50: list[float]
    p90: list[float]
    elapsed_ms: float


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Render the main page with a placeholder price chart."""
    return templates.TemplateResponse(request, "index.html")


@app.get("/health")
async def health(request: Request) -> dict:
    """Return liveness status and whether the forecasting model is loaded."""
    return {"status": "ok", "model_loaded": request.app.state.forecaster is not None}


@app.post("/forecast", dependencies=[Depends(require_api_key)])
def forecast(body: ForecastRequest, request: Request) -> ForecastResponse:
    """Forecast the next ``horizon`` hours from the given prices. Does not touch the database.

    This is a sync endpoint, so FastAPI runs it in a threadpool and the CPU-bound
    inference does not block the event loop.
    """
    forecaster = request.app.state.forecaster
    if forecaster is None:
        raise HTTPException(status_code=503, detail="model not loaded")
    result = forecaster.predict(body.prices, body.horizon)
    if not all(math.isfinite(x) for x in result.p10 + result.p50 + result.p90):
        raise HTTPException(status_code=500, detail="model returned non-finite values")
    return ForecastResponse(
        model=forecaster.model_name,
        context_length=min(len(body.prices), forecaster.max_context),
        horizon=body.horizon,
        p10=result.p10,
        p50=result.p50,
        p90=result.p90,
        elapsed_ms=result.elapsed_ms,
    )
