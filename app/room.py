"""Room size from the same sweep: the model points at corners, edges, openings and shelving in frames that show the floor sheet; code measures them."""

import asyncio
import io
import itertools
import json
from pathlib import Path
from statistics import median
from typing import Literal

import numpy as np
from PIL import Image
from pydantic import BaseModel, Field

from app import geometry
from app.config import settings
from app.llm import ask, image
from app.packet import M2_TO_FT2, Opening, Room
from app.read import Point, Reading

Frames = list[tuple[Reading, "Pointed", list[list[float]]]]

POINT = """<task>This frame is from a phone sweep of a room for an insurance claim. A sheet of A4 paper lies flat on the floor. Point at the room's geometry so it can be measured.</task>
<rules>
- Points are [y, x] on 0-1000 of the whole frame. Point only at what you can see in this frame; never guess a hidden point.
- sheet_visible: whether all four corners of the A4 sheet on the floor are in this frame. sheet: its four corners in order around it.
- corners: every place where two walls meet the floor, including one straight ahead in the middle of the frame. Say which walls meet there, by what is on them and their colour.
- edges: vertical lines on the walls where both the floor end and the ceiling end are visible, such as a corner between two walls; foot on the floor, top at the ceiling.
- openings: every door, window or open archway. For each, the floor point at the base of the wall straight below its left edge and below its right edge, and the bottom and top of its left edge (for a door the bottom is on the floor).
- units: every bookcase or shelving unit standing on the floor: the floor points under the front left and front right corners, and the top front left corner.
</rules>"""

ZOOM = """<task>Point at the four corners of the sheet of A4 paper in this close-up, in order around the sheet. Points are [y, x] on 0-1000 of this close-up. Put each point exactly on the paper's corner.</task>"""

PLAN = """<task>You turn measurements from several frames of one room into its floor plan. Every observation is in millimetres on the floor, measured from a sheet of paper lying on the floor.</task>
<rules>
- All frames share one floor frame: the same coordinates mean the same place. Use the descriptions and the coordinates together.
- corners: group the corner observations that are the same room corner, then list the room's corners in order around the room.
- missing: if exactly one corner of a four-cornered room was never seen, say so, and the plan completes it from the other three.
- openings and units: group the observations of the same door, window, archway or shelving unit.
- shape: one line on the room's shape (rectangle, L-shape, angled wall) and on any corner that was not seen.
- Use every observation at most once; leave out an observation that does not fit the room.
</rules>"""


class Edge(BaseModel):
    """A vertical line on a wall from floor to ceiling."""

    foot: Point
    top: Point
    where: str


class Corner(BaseModel):
    """Where two walls meet the floor."""

    point: Point
    walls: str


class Gap(BaseModel):
    """A door, window or archway in a wall."""

    kind: Literal["door", "window", "arch"]
    left_foot: Point
    right_foot: Point
    bottom: Point
    top: Point
    where: str


class Unit(BaseModel):
    """A shelving unit standing on the floor."""

    left_foot: Point
    right_foot: Point
    top: Point
    description: str


class Pointed(BaseModel):
    """What the model pointed at in one room frame."""

    sheet_visible: bool
    sheet: list[Point] = Field(min_length=4, max_length=4)
    corners: list[Corner]
    edges: list[Edge]
    openings: list[Gap]
    units: list[Unit]


class Zoomed(BaseModel):
    """The sheet's corners in a close-up."""

    corners: list[Point] = Field(min_length=4, max_length=4)


class Seen(BaseModel):
    """One measured observation in floor millimetres."""

    id: str
    n: int
    frame: str
    kind: Literal["corner", "opening", "unit"]
    what: str
    opening: str = ""
    x_mm: float
    y_mm: float
    width_mm: float | None = None
    height_mm: float | None = None
    bottom_mm: float | None = None


class Plan(BaseModel):
    """The plan model's grouping of observations into the room."""

    corners: list[list[str]]
    missing: bool
    openings: list[list[str]]
    units: list[list[str]]
    shape: str


async def zoom(run: Path, reading: Reading, rough: list[list[int]]) -> list[list[float]]:
    """Point at the sheet again in a close-up around its rough corners, for a finer fix."""
    ys, xs = [p[0] for p in rough], [p[1] for p in rough]
    pad = settings().crop_pad
    dy, dx = (max(ys) - min(ys)) * pad + 1, (max(xs) - min(xs)) * pad + 1
    box = [max(0, min(ys) - dy), max(0, min(xs) - dx), min(1000, max(ys) + dy), min(1000, max(xs) + dx)]
    f = reading.frame
    with Image.open(run / f.file) as im:
        crop = im.crop(
            (box[1] * f.width / 1000, box[0] * f.height / 1000, box[3] * f.width / 1000, box[2] * f.height / 1000)
        )
        out = io.BytesIO()
        crop.convert("RGB").save(out, "JPEG", quality=95)
    fine = await ask(Zoomed, [image(out.getvalue()), ZOOM], settings().geometry_model)
    return [[box[0] + y * (box[2] - box[0]) / 1000, box[1] + x * (box[3] - box[1]) / 1000] for y, x in fine.corners]


async def point(run: Path, reading: Reading) -> tuple[Reading, Pointed, list[list[float]]] | None:
    """The model's points in one room frame, with the sheet refined in a close-up; None when the sheet is not all in view."""
    data = (run / reading.frame.file).read_bytes()
    pointed = await ask(Pointed, [image(data), POINT], settings().geometry_model)
    if not pointed.sheet_visible or len({tuple(p) for p in pointed.sheet}) < 4:
        return None
    return reading, pointed, geometry.anticlockwise(await zoom(run, reading, pointed.sheet))


def views(frames: Frames, turns: dict[int, bool]) -> dict[int, geometry.View]:
    """Each frame's floor homography, with the sheet's labels turned half a turn where `turns` says so."""
    size = settings().reference_mm
    return {
        r.frame.n: geometry.sheet_view(
            fine[2:] + fine[:2] if turns.get(r.frame.n) else fine, r.frame.width, r.frame.height, size
        )
        for r, _, fine in frames
    }


def compass(frames: Frames, seen: dict[int, geometry.View], focal: float | None) -> dict[int, bool] | None:
    """Half-turns from the phone compass (either sign convention), used only if a Rayleigh test on doubled angles passes."""
    if not focal or len(frames) < 2 or any(r.frame.heading is None for r, _, _ in frames):
        return None
    fits = []
    for sign in (1, -1):
        angles = np.radians([sign * r.frame.heading + geometry.facing(seen[r.frame.n], focal) for r, _, _ in frames])
        total, n = np.exp(2j * angles).sum(), len(angles)
        p = float(np.exp(np.sqrt(1 + 4 * n + 4 * (n * n - abs(total) ** 2)) - (1 + 2 * n)))
        mean = np.angle(total) / 2
        fits.append((p, {r.frame.n: bool(np.cos(a - mean) < 0) for (r, _, _), a in zip(frames, angles, strict=True)}))
    p, turns = min(fits, key=lambda f: f[0])
    return turns if p < 1 - settings().scoring.confidence else None


def observe(
    frames: Frames, seen_views: dict[int, geometry.View], focal: float | None
) -> tuple[list[Seen], list[float]]:
    """Every observation in floor millimetres, and the ceiling heights."""
    seen, ceilings = [], []
    for r, p, _ in frames:
        view, n, ref = seen_views[r.frame.n], r.frame.n, r.frame.file
        for i, c in enumerate(p.corners):
            x, y = geometry.on_floor(c.point, view)
            seen.append(Seen(id=f"{n}.c{i}", n=n, frame=ref, kind="corner", what=c.walls, x_mm=x, y_mm=y))
        if focal:
            ceilings += [geometry.height(e.foot, e.top, view, focal) for e in p.edges]
        for i, g in enumerate(p.openings):
            left, right = geometry.on_floor(g.left_foot, view), geometry.on_floor(g.right_foot, view)
            top = geometry.height(g.left_foot, g.top, view, focal) if focal else None
            bottom = (
                0.0 if g.kind != "window" else geometry.height(g.left_foot, g.bottom, view, focal) if focal else None
            )
            seen.append(
                Seen(
                    id=f"{n}.o{i}",
                    n=n,
                    frame=ref,
                    kind="opening",
                    what=g.where,
                    opening=g.kind,
                    x_mm=(left[0] + right[0]) / 2,
                    y_mm=(left[1] + right[1]) / 2,
                    width_mm=float(np.hypot(left[0] - right[0], left[1] - right[1])),
                    height_mm=top - bottom if top is not None and bottom is not None else None,
                    bottom_mm=bottom,
                )
            )
        for i, u in enumerate(p.units):
            left, right = geometry.on_floor(u.left_foot, view), geometry.on_floor(u.right_foot, view)
            seen.append(
                Seen(
                    id=f"{n}.u{i}",
                    n=n,
                    frame=ref,
                    kind="unit",
                    what=u.description,
                    x_mm=(left[0] + right[0]) / 2,
                    y_mm=(left[1] + right[1]) / 2,
                    width_mm=float(np.hypot(left[0] - right[0], left[1] - right[1])),
                    height_mm=geometry.height(u.left_foot, u.top, view, focal) if focal else None,
                )
            )
    return seen, ceilings


def groups(plan: Plan) -> list[list[str]]:
    """Every non-empty group the plan made."""
    return [g for g in plan.corners + plan.openings + plan.units if g]


def checked(plan: Plan, seen: list[Seen]) -> None:
    """Refuse a plan that invents an observation or uses one twice."""
    used = [i for group in groups(plan) for i in group]
    if unknown := set(used) - {s.id for s in seen}:
        raise ValueError(f"unknown observations {sorted(unknown)}")
    if len(used) != len(set(used)):
        raise ValueError("an observation is used twice")


async def planned(seen: list[Seen]) -> Plan:
    """Ask the plan model to group the observations, retrying with the reason when its plan is refused."""
    data = json.dumps([s.model_dump(exclude={"n"}) for s in seen], ensure_ascii=False)
    error = ""
    for _ in range(settings().model_attempts):
        retry = [f"Your last answer was refused: {error}. Fix it."] if error else []
        plan = await ask(Plan, [PLAN, data, *retry], settings().judge_model)
        try:
            checked(plan, seen)
            return plan
        except ValueError as e:
            error = str(e)
    raise RuntimeError(f"the plan model could not group the room: {error}")


def spread(plan: Plan, seen: list[Seen]) -> float:
    """Millimetres from every grouped observation to its group's mean position."""
    by_id = {s.id: s for s in seen}
    total = 0.0
    for g in groups(plan):
        x, y = mean_point(g, by_id)
        total += sum(float(np.hypot(by_id[i].x_mm - x, by_id[i].y_mm - y)) for i in g)
    return total


def path(frames: Frames, turns: dict[int, bool], focal: float) -> float:
    """Millimetres the camera moves between frames taken one after another."""
    seen = views(frames, turns)
    spots = [geometry.position(seen[r.frame.n], focal) for r, _, _ in sorted(frames, key=lambda f: f[0].frame.t)]
    return float(sum(np.linalg.norm(b - a) for a, b in itertools.pairwise(spots)))


def steady(frames: Frames, focal: float) -> dict[int, bool]:
    """Half-turns that give the smoothest camera path through the frames in time order (two-state Viterbi)."""
    ordered = sorted(frames, key=lambda f: f[0].frame.t)
    spots = {
        turned: {
            n: geometry.position(v, focal)
            for n, v in views(ordered, {r.frame.n: turned for r, _, _ in ordered}).items()
        }
        for turned in (False, True)
    }
    cost = {False: (0.0, [False]), True: (np.inf, [True])}
    prev = ordered[0][0].frame.n
    for r, _, _ in ordered[1:]:
        n = r.frame.n
        cost = {
            s: min(
                (
                    (c + float(np.linalg.norm(spots[s][n] - spots[q][prev])), [*trail, s])
                    for q, (c, trail) in cost.items()
                ),
                key=lambda x: x[0],
            )
            for s in (False, True)
        }
        prev = n
    trail = min(cost.values(), key=lambda x: x[0])[1]
    return {r.frame.n: s for (r, _, _), s in zip(ordered, trail, strict=True)}


def matched(plan: Plan, frames: Frames, focal: float, turns: dict[int, bool]) -> dict[int, bool]:
    """Refine half-turns, frame by frame, so the camera path stays smooth and grouped observations meet."""

    def cost(trial: dict[int, bool]) -> float:
        """Camera travel plus the spread of the plan's groups, both in millimetres."""
        return path(frames, trial, focal) + spread(plan, observe(frames, views(frames, trial), focal)[0])

    best = cost(turns)
    improved = True
    while improved:
        improved = False
        for n in list(turns):
            trial = {**turns, n: not turns[n]}
            if (c := cost(trial)) < best:
                turns, best, improved = trial, c, True
    return turns


def mean_point(ids: list[str], by_id: dict[str, Seen]) -> tuple[float, float]:
    """The average floor position of a group of observations."""
    return sum(by_id[i].x_mm for i in ids) / len(ids), sum(by_id[i].y_mm for i in ids) / len(ids)


def middle(values: list[float | None]) -> float | None:
    """The median of the known values."""
    known = [v for v in values if v is not None]
    return median(known) if known else None


async def measure(run: Path, readings: list[Reading]) -> Room:
    """Measure the room from every frame where the reader saw the floor sheet."""
    floor = [r for r in readings if any(x and x.surface == "floor" for x in (r.spines.sheet, r.scene.sheet))]
    refs = [r.frame.file for r in floor]
    if not floor:
        return Room(scale_method="no frame showed the A4 sheet on the floor, so the room was not measured")
    frames = [f for f in await asyncio.gather(*(point(run, r) for r in floor)) if f]
    if not frames:
        return Room(scale_method="the A4 sheet on the floor was never wholly in view", frame_refs=refs)
    base = views(frames, {})
    focals = [v.focal_px for v in base.values() if v.focal_px]
    focal = median(focals) if focals else None
    turns = compass(frames, base, focal)
    by_compass = turns is not None
    if turns is None:
        turns = steady(frames, focal) if focal else {}
    seen, ceilings = observe(frames, views(frames, turns), focal)
    if len([s for s in seen if s.kind == "corner"]) < 3:
        return Room(
            scale_method="fewer than three floor corners were seen with the floor sheet in view", frame_refs=refs
        )
    plan = await planned(seen)
    if focal and not by_compass and (better := matched(plan, frames, focal, turns)) != turns:
        turns = better
        seen, ceilings = observe(frames, views(frames, turns), focal)
        plan = await planned(seen)
    by_id = {s.id: s for s in seen}
    points = [mean_point(g, by_id) for g in plan.corners if g]
    if plan.missing and len(points) == 3:
        a, b, c = points
        points.append((a[0] + c[0] - b[0], a[1] + c[1] - b[1]))
    if len(points) < 3:
        return Room(scale_method="the plan model found fewer than three room corners", frame_refs=refs)
    area, perimeter = geometry.polygon(points)
    length, width = geometry.extent(points)
    ceiling = middle(ceilings)
    openings = [
        Opening(
            kind=by_id[g[0]].opening,
            width_m=round(middle([by_id[i].width_mm for i in g]) / 1000, 2),
            height_m=round((middle([by_id[i].height_mm for i in g]) or 0) / 1000, 2),
            frame_ref=by_id[g[0]].frame,
        )
        for g in plan.openings
        if g and middle([by_id[i].width_mm for i in g])
    ]
    gross = perimeter * ceiling / 1e6 if ceiling else None
    cut = sum(o.width_m * o.height_m for o in openings if o.width_m * o.height_m >= settings().scoring.opening_min_m2)
    shelved = [
        middle([by_id[i].width_mm for i in g]) * middle([by_id[i].height_mm for i in g]) / 1e6
        for g in plan.units
        if g and middle([by_id[i].width_mm for i in g]) and middle([by_id[i].height_mm for i in g])
    ]
    net = gross - cut if gross is not None else None
    method = (
        f"A4 sheet on the floor wholly in view in {len(frames)} frames: floor points mapped to millimetres by the sheet's "
        f"homography (sheet corners refined in a close-up); frames aligned by {'the phone compass' if by_compass else 'the camera path and the features frames share'}; heights from "
        f"the camera pose with a focal length of {focal:.0f} px recovered from the same sheet"
        if focal
        else "the focal length could not be recovered from the sheet, so heights were not measured"
    )
    return Room(
        length_m=round(length / 1000, 2),
        width_m=round(width / 1000, 2),
        height_m=round(ceiling / 1000, 2) if ceiling else None,
        floor_area_m2=round(area / 1e6, 2),
        floor_area_ft2=round(area / 1e6 * M2_TO_FT2, 1),
        wall_area_m2=round(net, 2) if net is not None else None,
        wall_area_ft2=round(net * M2_TO_FT2, 1) if net is not None else None,
        gross_wall_area_m2=round(gross, 2) if gross is not None else None,
        shelved_wall_area_m2=round(sum(shelved), 2) if shelved else None,
        shelved_wall_area_ft2=round(sum(shelved) * M2_TO_FT2, 1) if shelved else None,
        shape=plan.shape,
        corners_m=[(round(x / 1000, 3), round(y / 1000, 3)) for x, y in points],
        openings=openings,
        scale_method=method,
        frame_refs=[f[0].frame.file for f in frames],
    )
