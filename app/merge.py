"""The judge model groups sightings across frames into physical books and objects; code checks each sighting is used once."""

import io
import json
import logging
from pathlib import Path
from typing import Literal

from PIL import Image
from pydantic import BaseModel

from app.config import settings
from app.llm import ask, image
from app.read import Reading

log = logging.getLogger(__name__)

TRACK = """<task>A policyholder is sweeping their home library with a phone, frame by frame in time order; neighbouring frames overlap. You keep the shelf inventory: place every book sighting from the new frames onto the books already logged, or log new books.</task>
<rules>
- frames: one entry per new frame; books lists, for each of that frame's sightings in order (id N.b0, N.b1, ...), the book it belongs to: a logged book's id, or for a book not logged yet a new id you make up, starting with "new"; "" for a rejected sighting. Every sighting gets exactly one entry.
- A sighting is the same book as a logged one when it sits at the same place on the same shelf: use the order along the row, the neighbours on both sides, the spine look, the text, the size and the pose together. The context frames show the last frames already placed, with their placements, so you can follow the pan. Two copies of one title side by side are two books.
- Reject a sighting only when it is not a book (a box, a file, a reflection) or is a misreading of part of another book's spine. Give the reason.
- Shelving units are U1, U2 ... in the order the sweep first reaches them; shelves are S1, S2 ... from the top of each unit. Books lying flat on top of a unit belong to a shelf named TOP of that unit; books on other furniture to a shelf named after that furniture.
- shelves: give the complete left-to-right order of every shelf that gained a book or whose order you correct, listing logged and new ids together.
- best: for any book whose clearest sighting (largest, sharpest, least cut off) is now a new one, that sighting.
- links: tie each open policyholder remark to the books it is about; exclude when they asked to leave those books or that shelf out of the claim. An exclusion stays open, so link it again to books the new frames add to what it names.
- unrelated: open remarks that are about no book (objects, the room, small talk). A remark about books not logged yet goes in neither list; it stays open for later frames.
</rules>"""

THINGS = """<task>A policyholder is sweeping a room of their home with a phone, frame by frame in time order; neighbouring frames overlap. You keep the inventory of everything in the room that is not a book: place every object sighting from the new frames on an object already logged, or log a new object.</task>
<rules>
- Every new sighting id is placed on exactly one object or rejected. A sighting of an object already logged goes to that object's id; an object not logged yet gets a new id you make up, starting with "new", and an entry in new_objects.
- Two sightings are the same object when its place in the room, appearance and category agree; the context frames show the last frames already placed, with their placements, so you can follow the pan. Two identical chairs side by side are two objects.
- Reject a sighting when it is part of another object (a cushion of a sofa, a shelf of a bookcase), a second box on the same thing in one frame, a fixed part of the building (door, window, pipe, socket, a built-in ceiling light), or a person. Give the reason.
- new_objects and revised: category, a one-line description (what it is, colour, material), brand_model only when some sighting read it. revised corrects a logged object when a new sighting shows it better.
- best: for any object whose clearest whole view is now a new sighting, that sighting.
- links: tie each open policyholder remark to the objects it is about; exclude when they asked to leave them out of the claim. An exclusion stays open, so link it again to objects the new frames add to what it names.
- unrelated: open remarks that are about no object (books, the room, small talk). A remark about objects not logged yet goes in neither list; it stays open for later frames.
</rules>"""


class Note(BaseModel):
    """Something the policyholder said that the agent logged during the sweep."""

    id: str
    t: float
    frame: int | None
    kind: Literal["exclude", "edition", "art", "correction", "other"]
    said: str
    about: str


class Group(BaseModel):
    """One physical book and its sightings."""

    id: str
    unit: str
    shelf: str
    position: int
    sightings: list[str]
    best: str
    notes: list[str]
    excluded_by: str


class Object(BaseModel):
    """One physical non-book object and its sightings."""

    id: str
    category: str
    description: str
    brand_model: str
    sightings: list[str]
    best: str
    notes: list[str]
    excluded_by: str


class Described(BaseModel):
    """What an object is, as the tracker describes it."""

    id: str
    category: str
    description: str
    brand_model: str


class Rejected(BaseModel):
    """A sighting that is not a separate book or object."""

    sighting: str
    reason: str


class Unit(BaseModel):
    """A shelving unit as the merge names it."""

    label: str
    description: str


class Placed(BaseModel):
    """A sighting placed on a book."""

    sighting: str
    book: str


class Order(BaseModel):
    """One shelf's books from left to right."""

    unit: str
    shelf: str
    books: list[str]


class Link(BaseModel):
    """A remark tied to the books it is about."""

    remark: str
    books: list[str]
    exclude: bool


class Seen(BaseModel):
    """The book of each sighting in one frame, in the frame's sighting order; empty for a rejected sighting."""

    frame: int
    books: list[str]


class Update(BaseModel):
    """The tracker's answer for one batch of new frames."""

    frames: list[Seen]
    rejected: list[Rejected]
    shelves: list[Order]
    units: list[Unit]
    best: list[Placed]
    links: list[Link]
    unrelated: list[str] = []

    @property
    def placed(self) -> list[Placed]:
        """Each placed sighting with its book."""
        return [Placed(sighting=f"{f.frame}.b{i}", book=b) for f in self.frames for i, b in enumerate(f.books) if b]


class Library(BaseModel):
    """Every physical book logged so far."""

    units: list[Unit] = []
    books: list[Group] = []
    rejected: list[Rejected] = []
    merged: list[int] = []
    remarks: list[str] = []

    def applied(self, update: Update, new: set[str], remarks: set[str]) -> tuple["Library", list[Group]]:
        """The inventory after a checked update, and the books the update created; the inventory itself is untouched."""
        lib = self.model_copy(deep=True)
        done = [p.sighting for p in update.placed] + [r.sighting for r in update.rejected]
        if stray := set(done) - new:
            raise ValueError(f"placed sightings that are not new: {sorted(stray)}")
        if missing := new - set(done):
            raise ValueError(f"new sightings left unplaced: {sorted(missing)}")
        if len(done) != len(set(done)):
            raise ValueError(f"sightings placed twice: {sorted({s for s in done if done.count(s) > 1})}")
        logged = {b.id: b for b in lib.books}
        created = {p.book for p in update.placed} - set(logged)
        ordered = [i for o in update.shelves for i in o.books]
        if len(ordered) != len(set(ordered)):
            raise ValueError(
                f"books on two shelves or twice on one: {sorted({i for i in ordered if ordered.count(i) > 1})}"
            )
        if unknown := set(ordered) - set(logged) - created:
            raise ValueError(f"shelf orders name books with no sighting: {sorted(unknown)}")
        if unshelved := created - set(ordered):
            raise ValueError(f"new books missing from the shelf orders: {sorted(unshelved)}")
        listed = {(o.unit, o.shelf) for o in update.shelves}
        if dropped := {b.id for b in lib.books if (b.unit, b.shelf) in listed} - set(ordered):
            raise ValueError(f"shelf orders leave out logged books: {sorted(dropped)}")
        if bad := ({link.remark for link in update.links} | set(update.unrelated)) - remarks:
            raise ValueError(f"unknown remarks {sorted(bad)}")
        names = {}
        for key in sorted(created):
            names[key] = f"B{len(lib.books) + 1}"
            book = Group(id=names[key], unit="", shelf="", position=0, sightings=[], best="", notes=[], excluded_by="")
            lib.books.append(book)
            logged[book.id] = book
        for p in update.placed:
            logged[names.get(p.book, p.book)].sightings.append(p.sighting)
        for p in update.best:
            book = logged.get(names.get(p.book, p.book))
            if book is None or p.sighting not in book.sightings:
                raise ValueError(f"best sighting {p.sighting} is not one of book {p.book}'s sightings")
            book.best = p.sighting
        for o in update.shelves:
            for position, key in enumerate(o.books, 1):
                book = logged[names.get(key, key)]
                book.unit, book.shelf, book.position = o.unit, o.shelf, position
        for link in update.links:
            for key in link.books:
                book = logged.get(names.get(key, key))
                if book is None:
                    raise ValueError(f"remark {link.remark} names an unknown book {key}")
                book.notes += [link.remark] * (link.remark not in book.notes)
                book.excluded_by = link.remark if link.exclude else book.excluded_by
        for book in lib.books:
            book.best = book.best or book.sightings[0]
        lib.units += update.units
        lib.rejected += update.rejected
        lib.remarks += sorted(settled(update.links, update.unrelated) - set(lib.remarks))
        return lib, [logged[names[k]] for k in sorted(created)]


class ObjectUpdate(BaseModel):
    """The object tracker's answer for one batch of new frames."""

    placed: list[Placed]
    rejected: list[Rejected]
    new_objects: list[Described]
    revised: list[Described]
    best: list[Placed]
    links: list[Link]
    unrelated: list[str] = []


class Contents(BaseModel):
    """Every physical non-book object logged so far."""

    objects: list[Object] = []
    rejected: list[Rejected] = []
    merged: list[int] = []
    remarks: list[str] = []

    def applied(self, update: ObjectUpdate, new: set[str], remarks: set[str]) -> tuple["Contents", list[Object]]:
        """The inventory after a checked update, and the objects it created; the inventory itself is untouched."""
        done = [p.sighting for p in update.placed] + [r.sighting for r in update.rejected]
        if stray := set(done) - new:
            raise ValueError(f"placed sightings that are not new: {sorted(stray)}")
        if missing := new - set(done):
            raise ValueError(f"new sightings left unplaced: {sorted(missing)}")
        if len(done) != len(set(done)):
            raise ValueError(f"sightings placed twice: {sorted({s for s in done if done.count(s) > 1})}")
        lib = self.model_copy(deep=True)
        logged = {o.id: o for o in lib.objects}
        described = {d.id: d for d in update.new_objects}
        created = {p.book for p in update.placed} - set(logged)
        if undescribed := created - set(described):
            raise ValueError(f"new objects without an entry in new_objects: {sorted(undescribed)}")
        if bad := ({link.remark for link in update.links} | set(update.unrelated)) - remarks:
            raise ValueError(f"unknown remarks {sorted(bad)}")
        names = {}
        for key in sorted(created):
            names[key] = f"I{len(lib.objects) + 1}"
            d = described[key]
            obj = Object(
                id=names[key],
                category=d.category,
                description=d.description,
                brand_model=d.brand_model,
                sightings=[],
                best="",
                notes=[],
                excluded_by="",
            )
            lib.objects.append(obj)
            logged[obj.id] = obj
        for p in update.placed:
            logged[names.get(p.book, p.book)].sightings.append(p.sighting)
        for d in update.revised:
            obj = logged.get(names.get(d.id, d.id))
            if obj is None:
                raise ValueError(f"revised names an unknown object {d.id}")
            obj.category, obj.description, obj.brand_model = d.category, d.description, d.brand_model
        for p in update.best:
            obj = logged.get(names.get(p.book, p.book))
            if obj is None or p.sighting not in obj.sightings:
                raise ValueError(f"best sighting {p.sighting} is not one of object {p.book}'s sightings")
            obj.best = p.sighting
        for link in update.links:
            for key in link.books:
                obj = logged.get(names.get(key, key))
                if obj is None:
                    raise ValueError(f"remark {link.remark} names an unknown object {key}")
                obj.notes += [link.remark] * (link.remark not in obj.notes)
                obj.excluded_by = link.remark if link.exclude else obj.excluded_by
        for obj in lib.objects:
            obj.best = obj.best or obj.sightings[0]
        lib.rejected += update.rejected
        lib.remarks += sorted(settled(update.links, update.unrelated) - set(lib.remarks))
        return lib, [logged[names[k]] for k in sorted(created)]


def settled(links: list[Link], unrelated: list[str]) -> set[str]:
    """Remarks a tracker answer closed: linked to something, or judged to be about something else."""
    return {link.remark for link in links} | set(unrelated)


def sighting_ids(readings: list[Reading], kind: str) -> dict[str, tuple[Reading, int]]:
    """Every sighting id with the reading and index it points to."""
    ids = {}
    for r in readings:
        found = r.spines.books if kind == "b" else r.scene.things
        ids |= {f"{r.frame.n}.{kind}{i}": (r, i) for i in range(len(found))}
    return ids


def thumbnail(run: Path, reading: Reading) -> bytes:
    """A frame scaled down for a model that compares many frames at once."""
    with Image.open(run / reading.frame.file) as im:
        im.thumbnail((settings().live_frame_px,) * 2)
        out = io.BytesIO()
        im.convert("RGB").save(out, "JPEG", quality=85)
        return out.getvalue()


def listing(readings: list[Reading], kind: str) -> list[dict]:
    """The sightings per frame as the merge prompt shows them."""
    rows = []
    for r in readings:
        found = r.spines.books if kind == "b" else r.scene.things
        rows.append(
            {
                "frame": r.frame.n,
                "t": r.frame.t,
                "sightings": [{"id": f"{r.frame.n}.{kind}{i}"} | s.model_dump() for i, s in enumerate(found)],
            }
        )
    return rows


def placements(library: Library) -> dict[str, str]:
    """Which book each placed sighting belongs to."""
    return {s: b.id for b in library.books for s in b.sightings}


async def track(
    run: Path, library: Library, readings: dict[int, Reading], batch: list[Reading], notes: list[Note]
) -> tuple[Library, list[Group]]:
    """Place one batch of new frames onto the inventory, sending the tracker its own mistakes back."""
    new = set(sighting_ids(batch, "b"))
    fresh = [n for n in notes if n.id not in library.remarks or n.kind == "exclude"]
    if not new and not fresh:
        return library.model_copy(update={"merged": library.merged + [r.frame.n for r in batch]}), []
    context = [readings[n] for n in library.merged[-settings().context_frames :]]
    where = placements(library)
    parts: list = []
    for r in context:
        parts += [f"Context frame {r.frame.n} at {r.frame.t:.1f} s, already placed:", image(thumbnail(run, r))]
    for r in batch:
        parts += [f"New frame {r.frame.n} at {r.frame.t:.1f} s:", image(thumbnail(run, r))]
    index = sighting_ids(list(readings.values()), "b")
    shown = [
        b.model_dump(include={"id", "unit", "shelf", "position", "excluded_by"}) | where_spine(index, b.best)
        for b in library.books
    ]
    data = {
        "inventory": shown,
        "context_frames": [
            {**row, "sightings": [s | {"book": where.get(s["id"], "")} for s in row["sightings"]]}
            for row in listing(context, "b")
        ],
        "new_frames": listing(batch, "b"),
        "open_remarks": [n.model_dump() for n in fresh],
    }
    parts += [TRACK, json.dumps(data, ensure_ascii=False)]
    error = ""
    for _ in range(settings().model_attempts):
        retry = [f"Your last answer was refused: {error}. Fix it."] if error else []
        try:
            update = await ask(Update, parts + retry, settings().judge_model)
            lib, created = library.applied(update, new, {n.id for n in fresh})
            lib.merged += [r.frame.n for r in batch]
            return lib, created
        except ValueError as e:
            error = str(e)
            log.warning("book tracker answer refused for frames %s: %s", [r.frame.n for r in batch], error)
    raise RuntimeError(f"the tracker could not place frames {[r.frame.n for r in batch]}: {error}")


def where_spine(index: dict[str, tuple[Reading, int]], sighting: str) -> dict:
    """The reader's view of one sighting, for the inventory the tracker sees."""
    reading, i = index[sighting]
    spine = reading.spines.books[i]
    return {"best_sighting": sighting} | spine.model_dump(include={"text", "look", "pose", "readable"})


async def things(
    run: Path, contents: Contents, readings: dict[int, Reading], batch: list[Reading], notes: list[Note]
) -> tuple[Contents, list[Object]]:
    """Place one batch of new frames onto the object inventory, sending the tracker its own mistakes back."""
    new = set(sighting_ids(batch, "t"))
    fresh = [n for n in notes if n.id not in contents.remarks or n.kind == "exclude"]
    if not new and not fresh:
        return contents.model_copy(update={"merged": contents.merged + [r.frame.n for r in batch]}), []
    context = [readings[n] for n in contents.merged[-settings().context_frames :]]
    where = {s: o.id for o in contents.objects for s in o.sightings}
    parts: list = []
    for r in context:
        parts += [f"Context frame {r.frame.n} at {r.frame.t:.1f} s, already placed:", image(thumbnail(run, r))]
    for r in batch:
        parts += [f"New frame {r.frame.n} at {r.frame.t:.1f} s:", image(thumbnail(run, r))]
    shown = {"id", "category", "description", "brand_model", "excluded_by"}
    data = {
        "inventory": [o.model_dump(include=shown) for o in contents.objects],
        "context_frames": [
            {**row, "sightings": [s | {"object": where.get(s["id"], "")} for s in row["sightings"]]}
            for row in listing(context, "t")
        ],
        "new_frames": listing(batch, "t"),
        "open_remarks": [n.model_dump() for n in fresh],
    }
    parts += [THINGS, json.dumps(data, ensure_ascii=False)]
    error = ""
    for _ in range(settings().model_attempts):
        retry = [f"Your last answer was refused: {error}. Fix it."] if error else []
        try:
            update = await ask(ObjectUpdate, parts + retry, settings().judge_model)
            lib, created = contents.applied(update, new, {n.id for n in fresh})
            lib.merged += [r.frame.n for r in batch]
            return lib, created
        except ValueError as e:
            error = str(e)
            log.warning("object tracker answer refused for frames %s: %s", [r.frame.n for r in batch], error)
    raise RuntimeError(f"the tracker could not place objects from frames {[r.frame.n for r in batch]}: {error}")
