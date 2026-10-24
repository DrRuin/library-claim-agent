"""The live agent: a Gemini Live session bridged to the phone, with tools that act on the sweep."""

import asyncio
import io
import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect
from google.genai import types
from PIL import Image

from app.claim import close
from app.config import Locale, settings
from app.llm import client, spent
from app.packet import Packet, Stage
from app.price import rate
from app.sweep import FAILURES, Sweep

log = logging.getLogger(__name__)

AUDIO, FRAME = 1, 2

SYSTEM = """<role>You are the voice of a home contents insurer. A policyholder is filming their home library with their phone, once, in one continuous pass, so you can inventory and value it for a contents claim. You see the camera at about one frame a second and hear them.</role>
<opening>
- Greet them in one sentence. Say the claim will be priced in {country}, {currency}, and ask them to confirm; call set_market once they confirm or name another country.
- Explain the sweep in two sentences: pan slowly along every shelf, holding still about a second at each section, then show the rest of the room, the walls and the floor; the sheets of A4 paper they placed are the measuring reference, so show them too.
</opening>
<during>
- Messages starting [camera] come from a model that checks every full-resolution frame. When one reports a problem, tell them at once in a few words what to do (slow down, step closer, tilt away from the glare), while they are still there.
- Messages starting [inventory] say what the background agents logged. Now and then say in a few words what you are logging, for example the shelf and a title or two. Never say a title, count or price that no [inventory] message gave you.
- If they skip a shelf or a wall you can see, ask them to go back to it within this same pass.
- When you see a painting, print, poster or framed photograph, ask once whether it is an original or a print, and call log_remark with kind art.
- When they correct you or tell you something you cannot see (a first edition, a signed copy, books that are not theirs, a shelf to skip), call log_remark with their words, then confirm in a few words.
- Never ask them to take books off the shelf, scan barcodes or photograph items one at a time.
- Keep every turn short: one or two sentences. They are walking.
</during>
<finish>
- When they say they are done, call finish_sweep and tell them the background agents are pricing and measuring, which takes a few minutes.
- A message starting [packet] carries the summary. Read it back plainly in under thirty seconds: counts, totals with the currency, the room's floor area, and how many lines need a person to review. Then say the claim packet is on their screen.
- A [first packet] message comes when the packet falls due while some books or items are still being identified or priced. Read it back the same way, say it is a first packet and the rest is still being checked, and that a final packet will follow on their screen.
- A [final packet] message after a first packet: say in one or two sentences what changed (books identified, totals) and that the final claim packet is on their screen.
</finish>"""

TOOLS = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="set_market",
                description="Set the country and currency the claim is priced in, after the policyholder confirms them.",
                parameters_json_schema={
                    "type": "object",
                    "properties": {
                        "country": {"type": "string", "description": "Country name."},
                        "currency": {"type": "string", "description": "ISO 4217 currency code, such as INR or GBP."},
                    },
                    "required": ["country", "currency"],
                },
            ),
            types.FunctionDeclaration(
                name="log_remark",
                description="Log something the policyholder said that changes the claim, tied to what the camera shows now.",
                parameters_json_schema={
                    "type": "object",
                    "properties": {
                        "kind": {
                            "type": "string",
                            "enum": ["exclude", "edition", "art", "correction", "other"],
                            "description": "exclude: leave books, a shelf or an item out of the claim; edition: first edition, signed, rare; art: original or print; correction: anything you got wrong.",
                        },
                        "said": {"type": "string", "description": "What they said, in their words."},
                        "about": {"type": "string", "description": "What it is about, as the camera shows it now."},
                    },
                    "required": ["kind", "said", "about"],
                },
            ),
            types.FunctionDeclaration(
                name="inventory",
                description="The counts the background agents have logged so far, per shelf.",
                parameters_json_schema={"type": "object", "properties": {}},
            ),
            types.FunctionDeclaration(
                name="finish_sweep",
                description="The policyholder has finished the sweep. Starts identification, pricing and measurement; the summary arrives later as a [packet] message.",
                parameters_json_schema={"type": "object", "properties": {}},
            ),
        ]
    )
]


def summary(packet: Packet, before: bool = False) -> str:
    """The figures the agent reads back, all taken from the packet; `before` says a first packet came before this one."""
    t, r, c = packet.totals, packet.room, packet.sweep.currency
    floor = f"{r.floor_area_m2:.1f} square metres" if r.floor_area_m2 else "not measured"
    head = "[packet]" if packet.final and not before else "[final packet]" if packet.final else "[first packet]"
    return (
        f"{head} {t.book_count} books: {t.books_identified} identified, {t.books_unidentified} unidentified, "
        f"{t.book_count - t.books_identified - t.books_unidentified} for appraisal. "
        f"Books replacement cost {t.books_replacement_cost:,.0f} {c}, used value {t.books_used_value:,.0f} {c}. "
        f"{len(packet.items)} other items, replacement {t.items_replacement_cost_low:,.0f} to {t.items_replacement_cost_high:,.0f} {c}. "
        f"Floor area {floor}. {len(packet.review_queue)} lines need a person to review; "
        f"{t.excluded_from_totals} lines are left out of the totals."
    )


class Bridge:
    """One phone connection: audio and frames in, the agent's voice and the inventory out."""

    def __init__(self, ws: WebSocket, sweep: Sweep) -> None:
        """Tie a phone socket to a sweep."""
        self.ws = ws
        self.sweep = sweep
        self.session: Any = None
        self.handle: str | None = None
        self.done = asyncio.Event()
        self.open = asyncio.Event()
        self.closing: asyncio.Task | None = None
        self.pending: set[asyncio.Task] = set()
        self.heading: float | None = None
        self.speaking = False
        self.advice = ""
        self.told: list[str] = []
        sweep.listeners.append(self.inventory_event)

    async def send(self, message: dict) -> None:
        """A JSON message to the phone, dropped if the phone has gone."""
        try:
            await self.ws.send_text(json.dumps(message, ensure_ascii=False, default=str))
        except (WebSocketDisconnect, RuntimeError):
            self.done.set()

    def config(self) -> types.LiveConnectConfig:
        """The live session setup, resuming the last session when there is a handle."""
        s = settings()
        return types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            system_instruction=SYSTEM.format(country=self.sweep.locale.country, currency=self.sweep.locale.currency),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=s.live_voice))
            ),
            tools=TOOLS,
            context_window_compression=types.ContextWindowCompressionConfig(
                trigger_tokens=s.live_compress_tokens, sliding_window=types.SlidingWindow()
            ),
            session_resumption=types.SessionResumptionConfig(handle=self.handle),
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
        )

    async def run(self) -> None:
        """Serve the phone until it hangs up."""
        phone = asyncio.create_task(self.phone())
        agent = asyncio.create_task(self.agent())
        await self.done.wait()
        for task in (phone, agent):
            task.cancel()
        if self.closing:
            await self.closing

    async def phone(self) -> None:
        """Read audio, frames and control messages from the phone."""
        try:
            while not self.done.is_set():
                message = await self.ws.receive()
                if message.get("bytes"):
                    data = message["bytes"]
                    if data[0] == AUDIO:
                        await self.realtime(audio=types.Blob(data=data[1:], mime_type="audio/pcm;rate=16000"))
                    elif data[0] == FRAME:
                        self.keep(self.frame(data[1:], self.heading))
                elif message.get("text"):
                    control = json.loads(message["text"])
                    if control.get("type") == "heading":
                        self.heading = control.get("value")
                    elif control.get("type") == "hello":
                        self.sweep.device = control.get("device", "")
                    elif control.get("type") == "finish":
                        await self.finish()
                elif message.get("type") == "websocket.disconnect":
                    break
        finally:
            self.done.set()

    def keep(self, work: Any) -> None:
        """Run work in the background and hold a reference until it ends."""
        task = asyncio.create_task(work)
        self.pending.add(task)
        task.add_done_callback(self.kept)

    def kept(self, task: asyncio.Task) -> None:
        """Forget a finished task and keep its failure for the review queue."""
        self.pending.discard(task)
        if not task.cancelled() and (error := task.exception()):
            log.error("live task failed", exc_info=error)
            self.sweep.errors.append(f"live: {type(error).__name__}: {error}")

    async def realtime(self, **part: Any) -> None:
        """Send audio, video or text to the live session if one is open; while it reconnects the part is dropped."""
        try:
            if self.session is not None:
                await self.session.send_realtime_input(**part)
        except FAILURES as e:
            log.debug("live input dropped: %s", e)

    async def context(self, text: str) -> None:
        """Add a line to the session's context without asking for a reply; dropped while it reconnects."""
        try:
            if self.session is not None:
                await self.session.send_client_content(
                    turns=types.Content(role="user", parts=[types.Part(text=text)]), turn_complete=False
                )
        except FAILURES as e:
            log.debug("live context dropped: %s", e)

    async def frame(self, data: bytes, heading: float | None) -> None:
        """Save a full-resolution frame, show the agent a small copy, and pass on the frame model's verdict."""
        with Image.open(io.BytesIO(data)) as im:
            width, height = im.size
            im.thumbnail((settings().live_frame_px,) * 2)
            small = io.BytesIO()
            im.convert("RGB").save(small, "JPEG", quality=80)
        await self.realtime(video=types.Blob(data=small.getvalue(), mime_type="image/jpeg"))
        t = (datetime.now(UTC) - self.sweep.started).total_seconds()
        n = len(self.sweep.frames)
        verdict = await self.sweep.frame(data, t, width, height, heading)
        view = verdict.view
        await self.send({"type": "check", "frame": n} | view.model_dump())
        problems = [p for p in view.problems if p != "no_spines"]
        if problems and problems != self.told:
            self.told = problems
            self.advice = f"[camera] {', '.join(problems)} in the frame showing {view.shows}. Suggested: {view.advice}"
            if not self.speaking:
                await self.coach()

    async def coach(self) -> None:
        """Pass the latest camera advice to the agent; older advice it never heard is dropped."""
        text, self.advice = self.advice, ""
        if text:
            await self.realtime(text=text)

    async def inventory_event(self, event: dict) -> None:
        """Forward what the background agents logged to the phone and, briefly, to the agent."""
        await self.send({"type": "inventory"} | event)
        if event["kind"] == "books":
            await self.context(f"[inventory] {event['count']} books logged so far; per shelf {event['shelves']}.")
        elif event["kind"] == "identified" and event["verdict"] == "identified":
            await self.context(f"[inventory] logged {event['title']} by {event['author']} on {event['shelf']}.")
        elif event["kind"] == "items":
            await self.context(f"[inventory] {event['count']} other items in the room logged.")

    async def agent(self) -> None:
        """Hold the live session open, resuming it when the server asks to reconnect or the connection fails."""
        s, failed = settings(), 0
        try:
            while not self.done.is_set():
                try:
                    async with client().aio.live.connect(model=s.live_model, config=self.config()) as session:
                        self.session = session
                        self.open.set()
                        log.info("live session open, resuming=%s", self.handle is not None)
                        try:
                            if self.handle is None:
                                await session.send_realtime_input(
                                    text="The policyholder has opened the app. Greet them."
                                )
                            if not await self.receive(session):
                                return
                            failed = 0
                        finally:
                            self.open.clear()
                            self.session = None
                            log.info("live session closed")
                except FAILURES as e:
                    failed += 1
                    log.warning("live session failed (%d in a row): %s", failed, e)
                    self.sweep.errors.append(f"live: {type(e).__name__}: {e}")
                    if failed > s.live_retries:
                        return
                    await asyncio.sleep(s.live_retry_s)
        finally:
            self.done.set()

    async def receive(self, session: Any) -> bool:
        """Play the agent's turns, run its tools; true when the server asked to reconnect."""
        while not self.done.is_set():
            async for message in session.receive():
                if (update := message.session_resumption_update) and update.resumable and update.new_handle:
                    self.handle = update.new_handle
                if message.usage_metadata:
                    self.bill(message.usage_metadata)
                if message.go_away is not None:
                    log.info("live session asked to reconnect in %s", message.go_away.time_left)
                    return True
                if message.tool_call:
                    for call in message.tool_call.function_calls:
                        self.keep(self.tool(session, call))
                content = message.server_content
                if content is None:
                    continue
                if content.interrupted:
                    await self.send({"type": "interrupted"})
                if message.data:
                    self.speaking = True
                    await self.ws.send_bytes(bytes([AUDIO]) + message.data)
                if content.turn_complete or content.interrupted:
                    self.speaking = False
                    await self.coach()
                if content.input_transcription and content.input_transcription.text:
                    await self.say("user", content.input_transcription.text)
                if content.output_transcription and content.output_transcription.text:
                    await self.say("agent", content.output_transcription.text)
        return False

    def bill(self, usage: types.UsageMetadata) -> None:
        """Add one live turn's reported tokens to the live agent stage, priced by modality."""
        now, prices = datetime.now(UTC), settings().live_prices
        began = self.sweep.started
        stage = self.sweep.stages.setdefault("live agent", Stage(name="live agent", started_at=began, finished_at=now))
        stage.calls += 1
        stage.finished_at = now
        text = types.MediaModality.TEXT
        sides = (
            (
                usage.prompt_tokens_details
                or [types.ModalityTokenCount(modality=text, token_count=usage.prompt_token_count or 0)],
                "input",
            ),
            (
                usage.response_tokens_details
                or [types.ModalityTokenCount(modality=text, token_count=usage.response_token_count or 0)],
                "output",
            ),
        )
        for details, side in sides:
            for d in details:
                kind = "IMAGE" if d.modality == types.MediaModality.VIDEO else str(d.modality.value)
                stage.tokens_in += d.token_count * (side == "input")
                stage.tokens_out += d.token_count * (side == "output")
                usd = d.token_count * getattr(prices.get(kind, prices["TEXT"]), side) / 1e6
                stage.usd += usd
                spent()[0] += usd

    async def say(self, who: str, text: str) -> None:
        """Show and log a piece of the conversation."""
        await self.send({"type": "transcript", "who": who, "text": text})
        with (self.sweep.run / "transcript.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps({"who": who, "text": text}, ensure_ascii=False) + "\n")

    async def tool(self, session: Any, call: types.FunctionCall) -> None:
        """Run one tool the agent called and answer it."""
        args = call.args or {}
        await self.send({"type": "tool", "name": call.name, "args": args})
        if call.name == "set_market":
            result = await self.market(args["country"], args["currency"])
        elif call.name == "log_remark":
            note = await self.sweep.note(args["kind"], args["said"], args["about"])
            result = {"logged": note.id}
        elif call.name == "inventory":
            identified = [v for v in self.sweep.identified.values() if v and v.identity.verdict == "identified"]
            result = {
                "books": len(self.sweep.library.books),
                "per_shelf": self.sweep.shelves(),
                "identified": len(identified),
            }
        elif call.name == "finish_sweep":
            await self.finish()
            result = {"status": "started", "frames": len(self.sweep.frames)}
        else:
            result = {"error": f"no tool named {call.name}"}
        await session.send_tool_response(
            function_responses=[types.FunctionResponse(id=call.id, name=call.name, response=result)]
        )

    async def market(self, country: str, currency: str) -> dict:
        """Price the claim in another market, carrying the appraisal limit over at today's rate."""
        old = self.sweep.locale
        code = currency.upper()
        fx, _ = await rate(old.currency, code)
        self.sweep.locale = Locale(country=country, currency=code, appraisal_limit=round(old.appraisal_limit * fx))
        await self.send({"type": "market", "country": country, "currency": code})
        return {"country": country, "currency": code}

    async def finish(self) -> None:
        """End the sweep once and read the packet back when it is ready."""
        if self.closing is None:
            self.sweep.ended = self.sweep.ended or datetime.now(UTC)
            await self.send({"type": "finishing"})
            self.closing = asyncio.create_task(self.report())

    async def report(self) -> None:
        """Wait for the packet, show it on the phone and give the agent its summary."""
        before = False
        async for packet in close(self.sweep):
            totals = packet.totals.model_dump()
            await self.send({"type": "packet", "run": self.sweep.run.name, "totals": totals, "final": packet.final})
            before = await self.tell(summary(packet, before)) or before

    async def tell(self, text: str) -> bool:
        """Give the agent a message, waiting through reconnects; false when no session took it in time."""
        s = settings()
        try:
            async with asyncio.timeout(s.live_wait_s):
                while True:
                    await self.open.wait()
                    if self.session is None:
                        continue
                    try:
                        await self.session.send_realtime_input(text=text)
                        return True
                    except FAILURES as e:
                        log.warning("the agent did not take the message, retrying: %s", e)
                        await asyncio.sleep(s.live_retry_s)
        except TimeoutError:
            log.warning("no live session took the message: %s", text)
            return False
