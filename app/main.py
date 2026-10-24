"""The web app: the sweep page and its socket, and every run's frames and packet."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from fastapi import FastAPI, WebSocket
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import ROOT, settings
from app.live import Bridge
from app.llm import web
from app.sweep import Sweep


class Client(BaseModel):
    """What the sweep page needs to know before it starts."""

    frame_seconds: float
    country: str
    currency: str


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Set up logging and close the shared HTTP client on shutdown."""
    logging.basicConfig(level=settings().log_level, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    yield
    await web().aclose()


app = FastAPI(title="Library claim agent", lifespan=lifespan)


@app.get("/api/client", response_model=Client)
async def client_settings() -> Client:
    """The capture interval and default market for the sweep page."""
    s = settings()
    return Client(frame_seconds=s.frame_seconds, country=s.locale.country, currency=s.locale.currency)


@app.websocket("/ws/sweep")
async def sweep(ws: WebSocket) -> None:
    """One sweep: the phone streams audio and frames, the agent talks back and the packet is built."""
    await ws.accept()
    run = settings().runs_dir / f"sweep-{datetime.now(UTC):%Y%m%d-%H%M%S}"
    await Bridge(ws, Sweep(run, settings().locale)).run()


settings().runs_dir.mkdir(parents=True, exist_ok=True)
app.mount("/runs", StaticFiles(directory=settings().runs_dir), name="runs")
app.mount("/", StaticFiles(directory=ROOT / "web", html=True), name="web")
