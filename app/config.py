"""Settings read once from the environment or `.env`."""

from functools import cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).parents[1]


class Scoring(BaseModel):
    """Pass bars from the brief, fixed before the holdout is opened."""

    count_tolerance: float = Field(0.05, description="Relative error allowed on the book count.")
    identified_bar: float = Field(0.70, description="Share of legible spines that must be identified.")
    wrong_bar: float = Field(0.03, description="Largest share of identified lines that may be wrong.")
    spine_tolerance: float = Field(0.15, description="Relative error allowed per spine dimension.")
    price_tolerance: float = Field(0.25, description="Relative error allowed per hand-checked price.")
    items_bar: float = Field(0.80, description="Share of truth items that must be found with the right category.")
    floor_tolerance: float = Field(0.10, description="Relative error allowed on floor area.")
    wall_tolerance: float = Field(0.15, description="Relative error allowed on net wall area.")
    opening_min_m2: float = Field(0, description="Openings at least this large come off wall area; NRM2 uses 1.0.")
    packet_seconds: float = Field(300, description="Seconds allowed from sweep end to packet.")
    confidence: float = Field(0.95, description="Confidence level of every interval.")


class Locale(BaseModel):
    """A market the packet is priced in."""

    country: str = Field(description="Country name as the policyholder would say it.")
    currency: str = Field(description="ISO 4217 code of the claim currency.")
    appraisal_limit: float = Field(description="Any line worth at least this much in `currency` goes to appraisal.")


class Price(BaseModel):
    """USD per million tokens for one model."""

    input: float
    output: float
    cached: float = 0


class Settings(BaseSettings):
    """Every setting the app reads, with its default."""

    model_config = SettingsConfigDict(
        env_file=ROOT / ".env", extra="ignore", env_nested_delimiter="__", env_ignore_empty=True
    )

    gemini_api_key: SecretStr | None = Field(None, description="Google AI key for every Gemini call.")
    reader_model: str = Field("gemini-4-argon", description="Reads spines and finds items in each saved frame.")
    judge_model: str = Field("gemini-4-argon", description="Merges sightings, identifies, prices and scores.")
    frame_model: str = Field("gemini-3.8-flash", description="Judges each captured frame fast during the sweep.")
    geometry_model: str = Field(
        "gemini-4-argon", description="Points at the room's corners, edges, openings and shelving."
    )
    price_model: str = Field("gemini-4-argon", description="Searches the web for new and used prices.")
    search_thinking: Literal["minimal", "low", "medium", "high"] | None = Field(
        None, description="Thinking level for grounded search calls; empty keeps the model's default."
    )
    check_model: str = Field("gemini-3.8-flash", description="Checks each price against its page.")
    live_model: str = Field("gemini-3.8-live", description="Talks with the policyholder during the sweep.")
    live_compress_tokens: int = Field(25_600, description="Live context size at which the oldest turns are compressed.")
    live_voice: str = Field("Kore", description="Prebuilt voice of the live agent.")
    model_attempts: int = Field(5, description="Tries per model call on 408, 429 and 5xx.")
    model_timeout_ms: int = Field(600_000, description="Milliseconds one model request may take.")
    budget_usd: float = Field(
        5, description="USD a process may spend on model calls at list prices; past it no request is sent."
    )
    concurrency: int = Field(96, description="Model calls in flight at once in the background pipeline.")
    prices: dict[str, Price] = Field(
        {
            "gemini-4-argon": Price(input=2.0, output=10.0, cached=0.10),
            "gemini-3.8-flash": Price(input=0.75, output=3.75, cached=0.075),
            "gemini-robotics-er-2-preview": Price(input=0.75, output=3.75, cached=0.075),
        },
        description="Paid-tier list prices from ai.google.dev/gemini-api/docs/pricing, checked 2026-10-24.",
    )
    search_usd: float = Field(0.014, description="USD per grounded search request past the free monthly quota.")
    live_prices: dict[str, Price] = Field(
        {
            "TEXT": Price(input=0.75, output=4.5),
            "AUDIO": Price(input=3.0, output=12.0),
            "IMAGE": Price(input=1.0, output=0),
        },
        description="Live model USD per 1M tokens by modality (pricing page, 2026-10-24); video frames bill as IMAGE.",
    )
    locale: Locale = Field(
        Locale(country="India", currency="INR", appraisal_limit=100_000), description="The claim's market."
    )
    second_locale: Locale = Field(
        Locale(country="United Kingdom", currency="GBP", appraisal_limit=1_000),
        description="The market the sample of books is priced in again, to show locale is a setting.",
    )
    review_confidence: float = Field(0.8, description="Identified lines below this confidence also go to review.")
    second_sample: int = Field(10, description="Books priced again in the second market.")
    reference_mm: tuple[float, float] = Field((210, 297), description="Short and long side of the reference sheet.")
    identify_crops: int = Field(4, description="Spine crops of one book sent to identification, clearest first.")
    context_frames: int = Field(2, description="Frames already placed that the tracker sees again with each batch.")
    crop_pad: float = Field(0.15, description="Share of a box's size added on each side when cropping it.")
    packet_due_s: float = Field(
        280,
        description="Seconds after the sweep ends when a packet is written from the work done; the final one follows.",
    )
    frame_seconds: float = Field(1.0, description="Seconds between full-resolution frames the phone uploads.")
    live_frame_px: int = Field(1024, description="Long side of the frames the live model sees.")
    live_retries: int = Field(5, description="Reconnects in a row the bridge tries after a live session fails.")
    live_retry_s: float = Field(2, description="Seconds before reconnecting a failed live session.")
    live_wait_s: float = Field(30, description="Seconds a packet read-back waits for a live session to reopen.")
    log_level: str = Field("INFO", description="Level of the app's own log lines on the server.")
    runs_dir: Path = Field(ROOT / "runs", description="One folder per sweep: frames, events and the packet.")
    fetch_seconds: float = Field(20, description="Seconds a source page fetch may take.")
    user_agent: str = Field(
        "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Mobile Safari/537.36",
        description="User agent for fetching source pages, as the policyholder's phone browser would send.",
    )
    accept: str = Field(
        "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", description="Accept header for source pages."
    )
    accept_language: str = Field("en-IN,en;q=0.9", description="Accept-Language header for source pages.")
    fx_url: str = Field("https://api.frankfurter.dev/v1", description="ECB reference rates (Frankfurter).")
    scoring: Scoring = Scoring()


@cache
def settings() -> Settings:
    """Read the settings once."""
    return Settings()
