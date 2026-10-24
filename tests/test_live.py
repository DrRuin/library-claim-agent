"""The live session is billed from the tokens it reports and survives a dropped connection."""

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.genai import errors, types

from app import live
from app.config import Locale
from app.live import Bridge
from app.sweep import Sweep

LOCALE = Locale(country="India", currency="INR", appraisal_limit=100_000)


def count(modality: types.MediaModality, tokens: int) -> types.ModalityTokenCount:
    """Tokens of one modality."""
    return types.ModalityTokenCount(modality=modality, token_count=tokens)


def test_live_turns_are_billed_by_modality(tmp_path: Path) -> None:
    """Audio and video in, audio out, over two turns; a turn without details bills its total as text."""
    bridge = Bridge(None, Sweep(tmp_path / "run", LOCALE))
    m = types.MediaModality
    bridge.bill(
        types.UsageMetadata(
            prompt_tokens_details=[count(m.AUDIO, 1_000_000), count(m.VIDEO, 1_000_000)],
            response_tokens_details=[count(m.AUDIO, 100_000)],
        )
    )
    bridge.bill(types.UsageMetadata(prompt_token_count=1_000_000))
    stage = bridge.sweep.stages["live agent"]
    assert round(stage.usd, 6) == round(3.0 + 1.0 + 1.2 + 0.75, 6)
    assert (stage.calls, stage.tokens_in, stage.tokens_out) == (2, 3_000_000, 100_000)


class Session:
    """A live session that drops on its first connection and stays up on the next."""

    def __init__(self, drops: bool) -> None:
        """Remember whether this connection fails."""
        self.drops = drops
        self.texts: list[str] = []

    async def send_realtime_input(self, text: str = "", **_: object) -> None:
        """Record what was sent; a dropped connection refuses it."""
        if self.drops:
            raise errors.APIError(1006, {"message": "Abnormal closure."})
        self.texts.append(text)

    async def receive(self):
        """Fail like a closed socket, or wait quietly."""
        if self.drops:
            raise errors.APIError(1006, {"message": "Abnormal closure."})
        await asyncio.Event().wait()
        yield


def test_dropped_session_reconnects_and_reads_the_packet_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The first connection fails without go_away; the bridge resumes and the packet still reaches the agent."""
    sessions = [Session(drops=True), Session(drops=False)]
    opened = iter(sessions)

    @asynccontextmanager
    async def connect(**_: object):
        """Hand out the next session."""
        yield next(opened)

    monkeypatch.setattr(
        live, "client", lambda: SimpleNamespace(aio=SimpleNamespace(live=SimpleNamespace(connect=connect)))
    )
    monkeypatch.setattr(live.settings(), "live_retry_s", 0)

    async def sweep() -> Bridge:
        """Run the bridge until the second session is open, then send it the packet."""
        bridge = Bridge(None, Sweep(tmp_path / "run", LOCALE))
        agent = asyncio.create_task(bridge.agent())
        assert await bridge.tell("[packet] 1 book")
        agent.cancel()
        return bridge

    bridge = asyncio.run(sweep())
    assert sessions[1].texts == ["The policyholder has opened the app. Greet them.", "[packet] 1 book"]
    assert len(bridge.sweep.errors) == 1 and bridge.sweep.errors[0].startswith("live: APIError: 1006")


class Phone:
    """A phone socket that takes everything."""

    async def send_bytes(self, data: bytes) -> None:
        """Take audio."""

    async def send_text(self, text: str) -> None:
        """Take a message."""


def test_camera_advice_waits_for_the_agent_to_finish_speaking(tmp_path: Path) -> None:
    """Advice that comes while the agent talks is held, and only the latest reaches it when its turn ends."""
    bridge = Bridge(Phone(), Sweep(tmp_path / "run", LOCALE))
    session = Session(drops=False)
    bridge.session = session
    audio = types.Part(inline_data=types.Blob(data=b"\0\0", mime_type="audio/pcm"))

    async def receive():
        """The agent speaks, two pieces of advice arrive, then its turn ends."""
        yield types.LiveServerMessage(server_content=types.LiveServerContent(model_turn=types.Content(parts=[audio])))
        for text in ("[camera] blur", "[camera] glare"):
            bridge.advice = text
            assert bridge.speaking and session.texts == []
        yield types.LiveServerMessage(server_content=types.LiveServerContent(turn_complete=True))
        bridge.done.set()

    session.receive = receive
    assert asyncio.run(bridge.receive(session)) is False
    assert session.texts == ["[camera] glare"]


def test_audio_to_a_dropping_session_does_not_end_the_sweep(tmp_path: Path) -> None:
    """A chunk sent while the connection drops is lost, and the phone loop goes on."""
    bridge = Bridge(Phone(), Sweep(tmp_path / "run", LOCALE))
    bridge.session = Session(drops=True)
    asyncio.run(bridge.realtime(audio=types.Blob(data=b"\0\0", mime_type="audio/pcm;rate=16000")))
    assert not bridge.done.is_set()
