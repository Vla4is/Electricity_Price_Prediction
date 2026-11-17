"""LT Electricity Price Forecast web application."""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.db import init_db

BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create the database schema on startup."""
    init_db()
    yield


app = FastAPI(title="LT Electricity Price Forecast", lifespan=lifespan)
templates = Jinja2Templates(directory=BASE_DIR / "templates")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> HTMLResponse:
    """Render the main page with a placeholder price chart."""
    return templates.TemplateResponse(request, "index.html")


@app.get("/health")
async def health() -> dict[str, str]:
    """Return a simple liveness status for monitoring."""
    return {"status": "ok"}
