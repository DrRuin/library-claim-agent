"""Each model call is sent once, and no request is sent once the process has spent its budget."""

import asyncio

import pytest
from google.genai import types

from app import llm
from app.config import settings


def reply(text: str) -> types.GenerateContentResponse:
    """A minimal model reply with token counts."""
    part = types.Part(text=text)
    return types.GenerateContentResponse(
        candidates=[types.Candidate(content=types.Content(parts=[part]))],
        usage_metadata=types.GenerateContentResponseUsageMetadata(
            prompt_token_count=1_000_000, candidates_token_count=0
        ),
    )


def fake(monkeypatch: pytest.MonkeyPatch, delay: float) -> list[int]:
    """Replace the Gemini client with one that answers after `delay` seconds and counts the requests it gets."""
    sent: list[int] = []

    async def generate_content(**_: object) -> types.GenerateContentResponse:
        """One request to the fake model."""
        sent.append(1)
        await asyncio.sleep(delay)
        return reply("ok")

    client = type("C", (), {"aio": type("A", (), {"models": type("M", (), {"generate_content": generate_content})})})
    monkeypatch.setattr(llm, "client", lambda: client)
    llm.spent()[0] = 0.0
    return sent


def test_slow_call_is_sent_once(monkeypatch: pytest.MonkeyPatch) -> None:
    """A slow call is awaited, never raced by a second billed copy."""
    sent = fake(monkeypatch, 0.2)
    answer = asyncio.run(llm.generate("gemini-4-argon", [], types.GenerateContentConfig()))
    assert answer.text == "ok"
    assert len(sent) == 1


def test_no_request_past_the_budget(monkeypatch: pytest.MonkeyPatch) -> None:
    """Once metered spend reaches the budget, the next call is refused before anything is sent."""
    sent = fake(monkeypatch, 0)
    monkeypatch.setattr(settings(), "budget_usd", 1.0)
    asyncio.run(llm.generate("gemini-4-argon", [], types.GenerateContentConfig()))
    assert llm.spent()[0] == pytest.approx(2.0)
    with pytest.raises(llm.OverBudget):
        asyncio.run(llm.generate("gemini-4-argon", [], types.GenerateContentConfig()))
    assert len(sent) == 1
