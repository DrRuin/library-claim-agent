"""One Gemini client; every judgement goes through `ask` with a Pydantic schema, metered per pipeline stage."""

import asyncio
import time
from contextvars import ContextVar
from datetime import UTC, datetime
from functools import cache

import httpx
from google import genai
from google.genai import types
from pydantic import BaseModel
from selectolax.lexbor import LexborHTMLParser

from app.config import settings
from app.packet import Stage

CURRENT: ContextVar[Stage | None] = ContextVar("stage", default=None)


class Source(BaseModel):
    """A web page the model's search or page reader opened."""

    title: str
    url: str


@cache
def client() -> genai.Client:
    """The shared Gemini client with retries on rate limits and server errors."""
    s = settings()
    if s.gemini_api_key is None:
        raise RuntimeError("GEMINI_API_KEY is not set in .env")
    return genai.Client(
        api_key=s.gemini_api_key.get_secret_value(),
        http_options=types.HttpOptions(
            timeout=s.model_timeout_ms, retry_options=types.HttpRetryOptions(attempts=s.model_attempts)
        ),
    )


@cache
def limiter() -> asyncio.Semaphore:
    """Caps the model calls in flight."""
    return asyncio.Semaphore(settings().concurrency)


def image(data: bytes, mime: str = "image/jpeg") -> types.Part:
    """An image part at the highest resolution the model reads."""
    return types.Part.from_bytes(data=data, mime_type=mime, media_resolution="MEDIA_RESOLUTION_HIGH")


def meter(model: str, usage: types.GenerateContentResponseUsageMetadata | None, searches: int) -> None:
    """Add one call's tokens and list-price cost to the current stage."""
    if usage is None:
        return
    s = settings()
    price = s.prices.get(model)
    cached = usage.cached_content_token_count or 0
    tokens_in = (usage.prompt_token_count or 0) + (usage.tool_use_prompt_token_count or 0)
    tokens_out = (usage.candidates_token_count or 0) + (usage.thoughts_token_count or 0)
    usd = (
        ((tokens_in - cached) * price.input + cached * price.cached + tokens_out * price.output) / 1e6
        + searches * s.search_usd
        if price
        else 0.0
    )
    spent()[0] += usd
    current = CURRENT.get()
    if current is None:
        return
    current.calls += 1
    current.finished_at = datetime.now(UTC)
    current.tokens_in += tokens_in
    current.tokens_out += tokens_out
    current.searches += searches
    current.usd += usd


class OverBudget(RuntimeError):
    """The process has spent its model budget, so no further request is sent."""


@cache
def spent() -> list[float]:
    """USD this process has spent on metered model calls, as a one-item box."""
    return [0.0]


async def once(model: str, parts: list, config: types.GenerateContentConfig) -> types.GenerateContentResponse:
    """One request inside a concurrency slot, refused before it is sent once the budget is spent."""
    async with limiter():
        if spent()[0] >= settings().budget_usd:
            raise OverBudget(f"model budget of {settings().budget_usd} USD reached; no request sent")
        return await client().aio.models.generate_content(model=model, contents=parts, config=config)


async def generate(model: str, parts: list, config: types.GenerateContentConfig) -> types.GenerateContentResponse:
    """One metered model call, sent once; a blocked prompt or an empty answer is an error."""
    started = time.perf_counter()
    reply = await once(model, parts, config)
    if current := CURRENT.get():
        current.call_seconds.append(round(time.perf_counter() - started, 1))
    if reply.prompt_feedback and reply.prompt_feedback.block_reason:
        raise RuntimeError(f"{model} blocked the request: {reply.prompt_feedback.block_reason}")
    grounding = reply.candidates[0].grounding_metadata if reply.candidates else None
    meter(model, reply.usage_metadata, len(grounding.web_search_queries or []) if grounding else 0)
    if not reply.text:
        raise RuntimeError(
            f"{model} returned no answer: {reply.candidates[0].finish_reason if reply.candidates else ''}"
        )
    return reply


async def ask[T: BaseModel](schema: type[T], parts: list[types.Part | str], model: str) -> T:
    """Send parts to a model and return its answer validated against the schema."""
    config = types.GenerateContentConfig(
        response_mime_type="application/json", response_json_schema=schema.model_json_schema()
    )
    return schema.model_validate_json((await generate(model, parts, config)).text)


async def search[T: BaseModel](schema: type[T], parts: list[types.Part | str], model: str) -> tuple[T, list[Source]]:
    """Ask with Google Search on; return the answer and the pages the search grounded it on."""
    config = types.GenerateContentConfig(
        tools=[types.Tool(google_search=types.GoogleSearch())],
        response_mime_type="application/json",
        response_json_schema=schema.model_json_schema(),
        thinking_config=types.ThinkingConfig(thinking_level=level) if (level := settings().search_thinking) else None,
    )
    reply = await generate(model, parts, config)
    meta = reply.candidates[0].grounding_metadata
    chunks = meta.grounding_chunks or [] if meta else []
    pages = [Source(title=c.web.title or "", url=c.web.uri) for c in chunks if c.web and c.web.uri]
    return schema.model_validate_json(reply.text), pages


class Page(BaseModel):
    """A source page as fetched: where its links landed and its readable text."""

    url: str
    text: str


async def fetch(url: str) -> Page:
    """Follow a source link and keep the page's title, price metadata, structured data and visible text."""
    try:
        reply = await web().get(url)
    except httpx.HTTPError:
        return Page(url=url, text="")
    if not reply.is_success or "html" not in reply.headers.get("content-type", ""):
        return Page(url=str(reply.url), text="")
    tree = LexborHTMLParser(reply.text)
    data = [node.text() for node in tree.css('script[type="application/ld+json"]')]
    meta = [
        f"{m.attributes.get('property') or m.attributes.get('name')}: {m.attributes.get('content')}"
        for m in tree.css("meta[content]")
    ]
    tree.strip_tags(["script", "style", "noscript", "svg", "template"], recursive=True)
    body = tree.body.text(separator="\n") if tree.body else ""
    return Page(url=str(reply.url), text="\n".join([*meta, *data, body]).strip())


async def reread[T: BaseModel](schema: type[T], prompt: str, page: Page) -> T:
    """A second, fast model checks a claim against one page: its fetched text, else the page opened by Gemini's reader."""
    if page.text:
        return await ask(schema, [prompt, f"<page_text>\n{page.text}\n</page_text>"], settings().check_model)
    config = types.GenerateContentConfig(
        tools=[types.Tool(url_context=types.UrlContext())],
        response_mime_type="application/json",
        response_json_schema=schema.model_json_schema(),
    )
    reply = await generate(settings().check_model, [prompt], config)
    return schema.model_validate_json(reply.text)


async def opened(urls: list[str]) -> set[str]:
    """Every page address the search was grounded on, before and after its redirects."""
    return set(urls) | {page.url for page in await asyncio.gather(*(fetch(url) for url in urls))}


@cache
def web() -> httpx.AsyncClient:
    """The shared HTTP client for source pages and exchange rates."""
    s = settings()
    headers = {"User-Agent": s.user_agent, "Accept": s.accept, "Accept-Language": s.accept_language}
    return httpx.AsyncClient(follow_redirects=True, timeout=s.fetch_seconds, headers=headers)
