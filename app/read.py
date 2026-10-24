"""Read one saved frame: the reader model finds every spine, every non-book object and any reference sheet in it."""

import asyncio
import io
from pathlib import Path
from typing import Annotated, Literal

from PIL import Image
from pydantic import BaseModel, Field

from app.config import settings
from app.llm import ask, image

Box = Annotated[list[int], Field(min_length=4, max_length=4, description="[ymin, xmin, ymax, xmax] on 0-1000.")]
Point = Annotated[list[int], Field(min_length=2, max_length=2, description="[y, x] on 0-1000.")]
CATEGORIES = Literal[
    "shelving",
    "furniture",
    "lighting",
    "art",
    "rug",
    "textile",
    "electronics",
    "appliance",
    "decor",
    "plant",
    "storage",
    "media",
    "other",
]

SPINES = """<task>You read one frame of a phone video in which a policyholder sweeps their home library for an insurance claim. Find every book in the frame and read its spine.</task>
<rules>
- One entry per physical book whose spine, or whose stacked edge, is at least partly in the frame. Books lying flat or stacked count, each book of a stack separately.
- Box each book's visible spine tightly; for a flat book, box the spine face you see.
- text: copy every word printed on the spine exactly as printed, in reading order. Leave it empty when nothing can be read. Never complete a word you cannot see.
- title, author, publisher: only what the spine itself prints and you can read. Leave a field empty when it is not printed or not readable. Do not use what you know about books to fill a field.
- readable: full when you can read the whole title, partial when only some words, none when nothing.
- row: shelf rows inside this frame from the top, 1 for the highest row with books.
- unit: a few words naming the piece of furniture the book stands on, the same words every time for the same piece.
- look: colour and design of the spine in a few words, so a later step can tell neighbours apart.
- cut_off: the spine runs past the edge of the frame.
- Report a flat sheet of A4 paper if all four of its corners are visible: its corners in order around the sheet and the surface it lies on.
</rules>"""

SCENE = """<task>You look at one frame of a phone video in which a policyholder sweeps a room of their home for an insurance contents claim. List every object in the frame that is not a book.</task>
<rules>
- One entry per physical object: shelving units, furniture, lamps, framed art and portraits, rugs, electronics, appliances, decor, plants, storage. Leave out books, walls, floors, doors, windows and people.
- Box each object tightly. Describe what can be seen: what it is, colour, material.
- brand_model: only a brand or model name you can read on the object in this frame; otherwise empty.
- art: true for paintings, prints, posters, photographs and portraits in a frame or on a wall.
- in_shelf_plane: true when the object sits in or on the bookcase at about the depth of the book spines, so it is as far from the camera as they are.
- Report a flat sheet of A4 paper if all four of its corners are visible: its corners in order around the sheet and the surface it lies on.
</rules>"""


class Sheet(BaseModel):
    """The reference sheet as the model points at it."""

    corners: list[Point] = Field(min_length=4, max_length=4, description="Corners in order around the sheet.")
    surface: Literal["shelf", "wall", "floor", "furniture"]


class Spine(BaseModel):
    """One book as seen in one frame."""

    box_2d: Box
    row: int
    unit: str
    pose: Literal["upright", "leaning", "flat"]
    readable: Literal["full", "partial", "none"]
    text: str
    title: str
    author: str
    publisher: str
    look: str
    cut_off: bool


class Spines(BaseModel):
    """Every book in one frame."""

    books: list[Spine]
    sheet: Sheet | None = None


class Thing(BaseModel):
    """One non-book object as seen in one frame."""

    box_2d: Box
    category: CATEGORIES
    description: str
    material: str
    brand_model: str
    art: bool
    in_shelf_plane: bool
    cut_off: bool


class Scene(BaseModel):
    """Every non-book object in one frame."""

    things: list[Thing]
    sheet: Sheet | None = None


class Frame(BaseModel):
    """One saved full-resolution frame of the sweep."""

    n: int
    file: str
    t: float
    width: int
    height: int
    heading: float | None = None


class Reading(BaseModel):
    """What the reader found in one frame."""

    frame: Frame
    spines: Spines
    scene: Scene


async def read(run: Path, frame: Frame) -> Reading:
    """Read the spines and the objects in one frame."""
    data = (run / frame.file).read_bytes()
    model = settings().reader_model
    spines, scene = await asyncio.gather(
        ask(Spines, [image(data), SPINES], model), ask(Scene, [image(data), SCENE], model)
    )
    return Reading(frame=frame, spines=spines, scene=scene)


def crop(run: Path, frame: Frame, box: list[int]) -> bytes:
    """A JPEG of one box from a saved frame, widened on each side by the crop margin."""
    pad = settings().crop_pad
    with Image.open(run / frame.file) as im:
        y0, x0, y1, x1 = box
        dy, dx = (y1 - y0) * pad, (x1 - x0) * pad
        left, top = max(0, (x0 - dx) * im.width / 1000), max(0, (y0 - dy) * im.height / 1000)
        right, bottom = min(im.width, (x1 + dx) * im.width / 1000), min(im.height, (y1 + dy) * im.height / 1000)
        out = io.BytesIO()
        im.crop((round(left), round(top), round(right), round(bottom))).convert("RGB").save(out, "JPEG", quality=92)
        return out.getvalue()
