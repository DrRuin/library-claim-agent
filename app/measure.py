"""Spine sizes in millimetres from the boxes the reader drew, scaled by objects of known size located in the same frame."""

import math
from statistics import median

from pydantic import BaseModel

from app.config import settings
from app.identify import Identified
from app.read import Frame, Reading, Sheet, Spine, Thing


class Anchor(BaseModel):
    """One known-size object in one frame and the scale it gives."""

    frame: int
    kind: str
    ref: str
    mm_per_px: float


class Size(BaseModel):
    """A book's measured spine with how it was measured."""

    height_mm: float | None
    thickness_mm: float | None
    method: str


class Scale(BaseModel):
    """Every anchor in the sweep and how well they agree."""

    anchors: list[Anchor]
    loo_error: float | None
    method: str


def sheet_scale(sheet: Sheet, frame: Frame) -> float:
    """Millimetres per pixel from the sheet's four corners and its known size, averaged over opposite sides."""
    points = [(x * frame.width / 1000, y * frame.height / 1000) for y, x in sheet.corners]
    sides = [math.dist(points[i], points[(i + 1) % 4]) for i in range(4)]
    short, long = sorted(((sides[0] + sides[2]) / 2, (sides[1] + sides[3]) / 2))
    mm_short, mm_long = settings().reference_mm
    return (mm_short / short + mm_long / long) / 2


def extent(spine: Spine, frame: Frame) -> tuple[float, float]:
    """Spine length and thickness in pixels; a flat book's spine runs across the frame."""
    y0, x0, y1, x1 = spine.box_2d
    tall, wide = (y1 - y0) * frame.height / 1000, (x1 - x0) * frame.width / 1000
    return (wide, tall) if spine.pose == "flat" else (tall, wide)


def anchors(
    readings: dict[int, Reading], books: list[Identified], where: dict[str, tuple[Reading, int]]
) -> list[Anchor]:
    """Every reference sheet on a shelf or wall, and every whole sighting of a book with a catalogue height."""
    found = [
        Anchor(
            frame=r.frame.n, kind="A4 sheet", ref=f"frame {r.frame.n}", mm_per_px=sheet_scale(r.spines.sheet, r.frame)
        )
        for r in readings.values()
        if r.spines.sheet and r.spines.sheet.surface != "floor"
    ]
    for b in books:
        if b.identity.verdict == "unidentified" or not b.identity.height_mm:
            continue
        for s in b.group.sightings:
            reading, i = where[s]
            spine = reading.spines.books[i]
            if not spine.cut_off and spine.pose != "leaning":
                length, _ = extent(spine, reading.frame)
                found.append(
                    Anchor(
                        frame=reading.frame.n,
                        kind="catalogue height",
                        ref=b.group.id,
                        mm_per_px=b.identity.height_mm / length,
                    )
                )
    return found


def frame_scale(frame: int, found: list[Anchor], leave_out: str = "") -> float | None:
    """The median scale of a frame's anchors, leaving out one book's own anchors."""
    here = [a.mm_per_px for a in found if a.frame == frame and a.ref != leave_out]
    return median(here) if here else None


def scale(found: list[Anchor]) -> Scale:
    """How far each catalogue anchor lands from its frame's other anchors."""
    errors = []
    for a in found:
        if a.kind == "catalogue height" and (other := frame_scale(a.frame, found, a.ref)):
            errors.append(abs(other - a.mm_per_px) / a.mm_per_px)
    kinds = sorted({a.kind for a in found})
    method = (
        f"{len(found)} anchors ({', '.join(kinds)}); each frame's millimetres per pixel is the median of the anchors in it"
        if found
        else "no known-size object was seen, so no spine was measured"
    )
    return Scale(anchors=found, loo_error=median(errors) if errors else None, method=method)


def size(book: Identified, where: dict[str, tuple[Reading, int]], found: list[Anchor]) -> Size:
    """Median spine height and thickness over the book's whole sightings in frames that have a scale."""
    heights, thicknesses = [], []
    for s in book.group.sightings:
        reading, i = where[s]
        spine = reading.spines.books[i]
        mm = frame_scale(reading.frame.n, found, book.group.id)
        if mm is None or spine.cut_off:
            continue
        length, thick = extent(spine, reading.frame)
        heights.append(length * mm)
        thicknesses.append(thick * mm)
    if not heights:
        return Size(height_mm=None, thickness_mm=None, method="no whole sighting in a frame with a known-size object")
    note = (
        "; leaning, so the box overstates it"
        if any(where[s][0].spines.books[where[s][1]].pose == "leaning" for s in book.group.sightings)
        else ""
    )
    return Size(
        height_mm=round(median(heights), 1),
        thickness_mm=round(median(thicknesses), 1),
        method=f"median of {len(heights)} sightings scaled by their frames' anchors, own catalogue height left out{note}",
    )


def item_size(sightings: list[tuple[Reading, Thing]], found: list[Anchor]) -> tuple[float | None, float | None, str]:
    """Median width and height in millimetres of an object's whole sightings in the shelf plane of a scaled frame."""
    widths, heights = [], []
    for reading, thing in sightings:
        mm = frame_scale(reading.frame.n, found)
        if mm is None or thing.cut_off or not thing.in_shelf_plane:
            continue
        y0, x0, y1, x1 = thing.box_2d
        widths.append((x1 - x0) * reading.frame.width / 1000 * mm)
        heights.append((y1 - y0) * reading.frame.height / 1000 * mm)
    if not widths:
        return None, None, "no whole sighting in the shelf plane of a frame with a known-size object"
    return round(median(widths), 1), round(median(heights), 1), f"median of {len(widths)} sightings in the shelf plane"
