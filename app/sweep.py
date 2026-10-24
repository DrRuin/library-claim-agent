"""One sweep at run time: frames arrive, and reading, tracking, identification and pricing run while the policyholder walks."""

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from google.genai import errors
from pydantic import BaseModel, ValidationError
from pydantic_core import to_jsonable_python
from websockets.exceptions import ConnectionClosed

from app import identify, merge, price, room
from app.config import Locale, settings
from app.frames import Check, View, check
from app.identify import Identified
from app.llm import CURRENT
from app.merge import Contents, Library, Note
from app.packet import Room, Stage
from app.read import Frame, Reading, Scene, Spines, read

FAILURES = (
    errors.APIError,
    httpx.HTTPError,
    ConnectionClosed,
    RuntimeError,
    ValueError,
    ValidationError,
    OSError,
    LookupError,
    AttributeError,
    TypeError,
)


class BookPrice(BaseModel):
    """One book's checked prices in one market."""

    book: str
    locale: Locale
    new: price.Priced | None
    used: price.Priced | None
    gaps: list[str]


class ItemPrice(BaseModel):
    """One object's status and checked offers."""

    item: str
    status: str
    offers: list[price.Priced]
    basis: str
    gaps: list[str]


def whole(reading: Reading, index: int) -> bool:
    """Whether the reader read a sighting's whole title on a spine that lies fully inside the frame."""
    spine = reading.spines.books[index]
    return spine.readable == "full" and not spine.cut_off


class Sweep:
    """The state and background work of one sweep."""

    def __init__(self, run: Path, locale: Locale, device: str = "") -> None:
        """Open a sweep folder; nothing runs until the first frame."""
        self.run = run
        self.locale = locale
        self.device = device
        self.started = datetime.now(UTC)
        self.ended: datetime | None = None
        self.frames: list[Frame] = []
        self.checks: dict[int, Check] = {}
        self.readings: dict[int, Reading] = {}
        self.library = Library()
        self.identified: dict[str, Identified | None] = {}
        self.prices: dict[tuple[str, str], BookPrice] = {}
        self.contents = Contents()
        self.item_prices: dict[str, ItemPrice | None] = {}
        self.second: set[str] = set()
        self.turns: dict[str, int] = {}
        self.notes: list[Note] = []
        self.room = Room(scale_method="the room stage did not finish")
        self.stages: dict[str, Stage] = {}
        self.errors: list[str] = []
        self.tasks: set[asyncio.Task] = set()
        self.listeners: list[Callable[[dict], Awaitable[None]]] = []
        self.merging = asyncio.Lock()
        self.placing = asyncio.Lock()
        for folder in ("frames", "checks", "readings"):
            (run / folder).mkdir(parents=True, exist_ok=True)

    def meter(self, name: str) -> Stage:
        """The stage every model call in the current task is billed to."""
        now = datetime.now(UTC)
        stage = self.stages.setdefault(name, Stage(name=name, started_at=now, finished_at=now))
        CURRENT.set(stage)
        return stage

    def spawn(self, name: str, work: Awaitable[Any]) -> None:
        """Run background work under a stage, keeping any failure for the review queue."""

        async def guarded() -> None:
            """Run the work and record its failure instead of losing it."""
            stage = self.meter(name)
            try:
                await work
            except FAILURES as e:
                self.errors.append(f"{name}: {type(e).__name__}: {e}")
                await self.emit({"kind": "error", "stage": name, "message": str(e)})
            finally:
                stage.finished_at = datetime.now(UTC)

        task = asyncio.create_task(guarded())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def emit(self, event: dict) -> None:
        """Tell every listener (the phone page, the live agent) what changed, and log it."""
        event = {"t": (datetime.now(UTC) - self.started).total_seconds()} | event
        with (self.run / "events.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
        for listener in self.listeners:
            try:
                await listener(event)
            except FAILURES as e:
                self.errors.append(f"listener: {type(e).__name__}: {e}")

    def save(self, name: str, model: BaseModel | list | dict) -> None:
        """Write one stage output next to the frames."""
        data = to_jsonable_python(model)
        (self.run / name).write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")

    async def frame(self, data: bytes, t: float, width: int, height: int, heading: float | None = None) -> Check:
        """Save a full-resolution frame, judge it at once, and queue it for reading."""
        n = len(self.frames)
        frame = Frame(n=n, file=f"frames/{n:05d}.jpg", t=t, width=width, height=height, heading=heading)
        (self.run / frame.file).write_bytes(data)
        self.frames.append(frame)
        self.meter("frame check")
        try:
            verdict = await check(data)
        except FAILURES as e:
            self.errors.append(f"frame check: frame {frame.file} was not checked: {type(e).__name__}: {e}")
            verdict = Check(width=width, height=height, view=View(readable=True, problems=[], advice="", shows=""))
        self.checks[n] = verdict
        self.save(f"checks/{n:05d}.json", verdict)
        self.spawn("read", self.read(frame, verdict))
        return verdict

    async def read(self, frame: Frame, verdict: Check) -> None:
        """Read a frame unless the frame model found it blurred or dark, then let the tracker catch up; a failed read counts as empty."""
        reading = Reading(frame=frame, spines=Spines(books=[]), scene=Scene(things=[]))
        if not {"blur", "too_dark"} & set(verdict.view.problems):
            try:
                reading = await read(self.run, frame)
            except FAILURES as e:
                self.errors.append(f"read: frame {frame.file} was not read: {type(e).__name__}: {e}")
        self.readings[frame.n] = reading
        self.save(f"readings/{frame.n:05d}.json", reading)
        self.spawn("track", self.track())

    def unplaced(self, merged: list[int]) -> list[Reading]:
        """Read frames after the last placed one, in order, up to the first frame still being read."""
        done = set(merged)
        batch = []
        for f in self.frames:
            if f.n in done:
                continue
            if f.n not in self.readings:
                break
            batch.append(self.readings[f.n])
        return batch

    async def track(self) -> None:
        """Bring the book and object inventories up to the frames read so far."""
        await asyncio.gather(self.track_books(), self.track_things())

    async def track_books(self) -> None:
        """Place new frames on the shelves, then identify books seen whole or left by the pan, and re-identify remarked ones."""
        async with self.merging:
            batch = self.unplaced(self.library.merged)
            if not batch:
                return
            before = {b.id: (len(b.sightings), len(b.notes)) for b in self.library.books}
            self.library, created = await merge.track(self.run, self.library, self.readings, batch, self.notes)
            self.save("library.json", self.library)
            await self.emit(
                {"kind": "books", "count": len(self.library.books), "new": len(created), "shelves": self.shelves()}
            )
            where = self.where()
            for book in self.library.books:
                seen, remarked = before.get(book.id, (0, 0))
                ready = seen == len(book.sightings) or any(whole(*where[s]) for s in book.sightings)
                if (book.id not in self.identified and ready) or (
                    book.id in self.identified and remarked != len(book.notes)
                ):
                    self.start(book.id)

    async def track_things(self) -> None:
        """Place new frames on the object inventory, then price objects the pan has left and re-price remarked ones."""
        async with self.placing:
            batch = self.unplaced(self.contents.merged)
            if not batch:
                return
            before = {o.id: (len(o.sightings), len(o.notes)) for o in self.contents.objects}
            self.contents, created = await merge.things(self.run, self.contents, self.readings, batch, self.notes)
            self.save("contents.json", self.contents)
            if created:
                await self.emit(
                    {"kind": "items", "count": len(self.contents.objects), "new": [o.description for o in created]}
                )
            for obj in self.contents.objects:
                seen, remarked = before.get(obj.id, (0, 0))
                left = obj.id not in self.item_prices and seen == len(obj.sightings)
                if left or (obj.id in self.item_prices and remarked != len(obj.notes)):
                    self.item_prices[obj.id] = None
                    self.spawn("item price", self.item_price(obj.id))

    def shelves(self) -> dict[str, int]:
        """Books logged per shelf."""
        counts: dict[str, int] = {}
        for b in self.library.books:
            counts[f"{b.unit}-{b.shelf}"] = counts.get(f"{b.unit}-{b.shelf}", 0) + 1
        return counts

    def where(self) -> dict[str, tuple[Reading, int]]:
        """Every book sighting id with its reading."""
        return merge.sighting_ids(list(self.readings.values()), "b")

    def where_things(self) -> dict[str, tuple[Reading, int]]:
        """Every object sighting id with its reading."""
        return merge.sighting_ids([self.readings[n] for n in sorted(self.readings)], "t")

    def start(self, book_id: str) -> None:
        """Identify a book afresh; work started for it earlier no longer writes its results."""
        self.turns[book_id] = self.turns.get(book_id, 0) + 1
        self.identified[book_id] = None
        self.spawn("identify", self.identify(book_id, self.turns[book_id]))

    async def identify(self, book_id: str, turn: int) -> None:
        """Identify one book and find its offers in one search, then check the offers on their pages."""
        book = next(b for b in self.library.books if b.id == book_id)
        found = await identify.identify(self.run, book, self.where(), self.notes, self.locale)
        if self.turns[book_id] != turn:
            return
        self.identified[book_id] = found
        self.save("identified.json", {k: v for k, v in self.identified.items() if v})
        await self.emit(
            {"kind": "identified", "book": book_id, "shelf": f"{book.unit}-{book.shelf}", "position": book.position}
            | found.identity.model_dump(include={"verdict", "title", "author", "confidence"})
        )
        if found.identity.verdict == "identified":
            self.spawn("price", self.price(book_id, self.locale, turn))
            if len(self.second) < settings().second_sample and book_id not in self.second:
                self.second.add(book_id)
                self.spawn("second market", self.price(book_id, settings().second_locale, turn))

    async def price(self, book_id: str, locale: Locale, turn: int) -> None:
        """Check the offers the identifying search found in the claim market, then search afresh for any still missing."""
        found = self.identified[book_id]
        if found is None or self.turns[book_id] != turn:
            return
        identity = found.identity
        query = identity.model_dump(include={"title", "author", "publisher", "edition", "isbn"})
        new = used = None
        why = {"replacement": "", "used": ""}
        if locale == self.locale and identity.new and identity.used:
            offers = price.BookOffers(new=identity.new, used=identity.used)
            new, used, why = await price.checked(query, offers, found.grounded, locale)
        if new is None or used is None:
            try:
                again, used_again, why_again = await price.book(query, locale)
            except FAILURES as e:
                again = used_again = None
                why_again = dict.fromkeys(why, f"the price search failed: {type(e).__name__}: {e}")
            why = {k: "; then ".join(filter(None, (why[k], why_again[k]))) for k in why}
            new, used = new or again, used or used_again
        if self.turns[book_id] != turn:
            return
        gaps = [f"{k}: {why[k]}" for k, p in (("replacement", new), ("used", used)) if p is None]
        self.prices[(book_id, locale.currency)] = BookPrice(book=book_id, locale=locale, new=new, used=used, gaps=gaps)
        self.save("prices.json", list(self.prices.values()))
        if locale == self.locale:
            await self.emit(
                {"kind": "priced", "book": book_id, "title": identity.title, "currency": locale.currency}
                | {"new": new.amount if new else None, "used": used.amount if used else None}
            )

    async def note(self, kind: str, said: str, about: str) -> Note:
        """Log a policyholder remark against the latest frame."""
        note = Note(
            id=f"R{len(self.notes) + 1}",
            t=(datetime.now(UTC) - self.started).total_seconds(),
            frame=self.frames[-1].n if self.frames else None,
            kind=kind,
            said=said,
            about=about,
        )
        self.notes.append(note)
        self.save("notes.json", self.notes)
        await self.emit({"kind": "note"} | note.model_dump())
        return note

    async def settle(self) -> None:
        """Wait until no background work is left."""
        while self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    async def finish(self) -> None:
        """Drain the sweep: read and place every frame, identify and price every book and object."""
        self.ended = self.ended or datetime.now(UTC)
        await self.settle()
        self.spawn("track", self.track())
        self.spawn("room", self.measure())
        await self.settle()
        while left := [b.id for b in self.library.books if b.id not in self.identified] + [
            o.id for o in self.contents.objects if o.id not in self.item_prices
        ]:
            for key in left:
                if key in {b.id for b in self.library.books}:
                    self.start(key)
                else:
                    self.item_prices[key] = None
                    self.spawn("item price", self.item_price(key))
            await self.settle()
            self.spawn("track", self.track())
            await self.settle()

    async def measure(self) -> None:
        """Measure the room from the frames that show the floor sheet."""
        self.room = await room.measure(self.run, [self.readings[n] for n in sorted(self.readings)])
        (self.run / "room.json").write_text(self.room.model_dump_json(indent=1), encoding="utf-8")

    async def item_price(self, item_id: str) -> None:
        """Price one object in the claim market."""
        obj = next(o for o in self.contents.objects if o.id == item_id)
        remarks = [n.model_dump() for n in self.notes if n.id in obj.notes]
        thing = obj.model_dump(include={"category", "description", "brand_model"})
        status, offers, basis, gaps = await price.item(thing, remarks, self.locale)
        self.item_prices[item_id] = ItemPrice(item=item_id, status=status, offers=offers, basis=basis, gaps=gaps)
        self.save("item_prices.json", [p for p in self.item_prices.values() if p])
        await self.emit({"kind": "item priced", "item": item_id, "description": obj.description, "status": status})
