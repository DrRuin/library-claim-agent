"""Frames of a 4 x 6 m room with the floor sheet in view measure the floor, walls, door, window and bookcase, however each frame labels the sheet."""

import asyncio
from pathlib import Path

import numpy as np
import pytest

from app import geometry, room
from app.read import Frame, Reading, Scene, Sheet, Spines

W, H, F = 2160, 3840, 2900.0
X0, X1, Y0, Y1, CEILING = -1200.0, 2800.0, -1500.0, 4500.0, 2500.0
SHEET = [(0.0, 0.0, 0.0), (210.0, 0.0, 0.0), (210.0, 297.0, 0.0), (0.0, 297.0, 0.0)]
DOOR = [(500.0, Y1, 0.0), (1400.0, Y1, 0.0), (500.0, Y1, 0.0), (500.0, Y1, 2000.0)]
WINDOW = [(X1, 1000.0, 0.0), (X1, 2000.0, 0.0), (X1, 1000.0, 1000.0), (X1, 1000.0, 2100.0)]
CASE = [(-200.0, Y0, 0.0), (1600.0, Y0, 0.0), (-200.0, Y0, 2000.0)]
CORNERS = [(X0, Y0), (X1, Y0), (X1, Y1), (X0, Y1)]
NAMES = ("c0", "c1", "c2", "c3")
WALLS = 2 * ((X1 - X0) + (Y1 - Y0)) * CEILING / 1e6 - 0.9 * 2.0 - 1.0 * 1.1


def look(centre: tuple, target: tuple) -> tuple[np.ndarray, np.ndarray, float]:
    """Rotation and translation of a phone at centre looking at target, and its compass heading."""
    c, forward = np.array(centre, float), np.array(target, float) - np.array(centre, float)
    forward /= np.linalg.norm(forward)
    right = np.cross(forward, [0, 0, 1.0])
    right /= np.linalg.norm(right)
    r = np.vstack([right, np.cross(forward, right), forward])
    return r, -r @ c, float((70 - np.degrees(np.arctan2(forward[1], forward[0]))) % 360)


def project(cam: tuple, point: tuple, exact: bool = False) -> list:
    """The [y, x] point on 0-1000 where a room point lands, rounded as the model gives it unless exact."""
    r, t, _ = cam
    x, y, z = r @ np.array(point) + t
    out = [(F * y / z + H / 2) / H * 1000, (F * x / z + W / 2) / W * 1000]
    return out if exact else [round(v) for v in out]


def gap(cam: tuple, kind: str, points: list) -> room.Gap:
    """A door or window as a perfect model points at it."""
    left, right, bottom, top = (project(cam, p) for p in points)
    return room.Gap(kind=kind, left_foot=left, right_foot=right, bottom=bottom, top=top, where=kind)


def shot(n: int, centre: tuple, target: tuple, shows: list[str], headed: int) -> tuple:
    """One frame as a perfect pointing model and close-up return it, the sheet listed from a frame-dependent start."""
    cam = look(centre, target)
    order = SHEET[n % 4 :] + SHEET[: n % 4]
    order = order[::-1] if n % 3 == 1 else order
    frame = Frame(
        n=n,
        file=f"frames/{n:05d}.jpg",
        t=n,
        width=W,
        height=H,
        heading=(cam[2] if headed > 0 else -cam[2]) if headed else None,
    )
    sheet = Sheet(corners=[project(cam, p) for p in order], surface="floor")
    corners = [CORNERS[NAMES.index(s)] for s in shows if s in NAMES]
    pointed = room.Pointed(
        sheet_visible=True,
        sheet=sheet.corners,
        corners=[room.Corner(point=project(cam, (*c, 0)), walls="two walls") for c in corners],
        edges=[room.Edge(foot=project(cam, (*c, 0)), top=project(cam, (*c, CEILING)), where="corner") for c in corners],
        openings=[gap(cam, s, DOOR if s == "door" else WINDOW) for s in shows if s in ("door", "window")],
        units=[
            room.Unit(
                left_foot=project(cam, CASE[0]),
                right_foot=project(cam, CASE[1]),
                top=project(cam, CASE[2]),
                description="white bookcase",
            )
        ]
        if "case" in shows
        else [],
    )
    reading = Reading(frame=frame, spines=Spines(books=[], sheet=sheet), scene=Scene(things=[]))
    return reading, pointed, geometry.anticlockwise([project(cam, p, exact=True) for p in order])


def plan(shots: dict[int, list[str]]) -> room.Plan:
    """The grouping a correct plan model makes: every sighting of the same thing together, corners in order."""
    ids: dict[str, list[str]] = {}
    for n, shows in shots.items():
        kinds = {"c": [s for s in shows if s in NAMES], "o": [s for s in shows if s in ("door", "window")]}
        kinds["u"] = [s for s in shows if s == "case"]
        for letter, names in kinds.items():
            for i, name in enumerate(names):
                ids.setdefault(name, []).append(f"{n}.{letter}{i}")
    return room.Plan(
        corners=[ids[f"c{i}"] for i in range(4) if f"c{i}" in ids],
        missing=False,
        openings=[ids[k] for k in ("door", "window") if k in ids],
        units=[ids["case"]] if "case" in ids else [],
        shape="rectangle",
    )


def measured(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, poses: list, headed: int) -> room.Room:
    """The room measured from the posed frames with a plan model that groups correctly."""
    frames = {n: shot(n, c, t, shows, headed) for n, (c, t, shows) in enumerate(poses)}
    grouping = plan({n: shows for n, (_, _, shows) in enumerate(poses)})

    async def point(run: Path, reading: Reading) -> tuple:
        """The perfect pointing for this frame."""
        return frames[reading.frame.n]

    async def ask(schema: type, parts: list, model: str) -> room.Plan:
        """The correct grouping."""
        return grouping

    monkeypatch.setattr(room, "point", point)
    monkeypatch.setattr(room, "ask", ask)
    return asyncio.run(room.measure(tmp_path, [f[0] for f in frames.values()]))


def corner_view(i: int) -> tuple:
    """A phone 1.3 m behind the sheet looking past it towards corner i."""
    d = np.array(CORNERS[i]) - [105, 148]
    u = d / np.linalg.norm(d)
    return (105 - 1300 * u[0], 148 - 1300 * u[1], 1400.0), (105 + 0.35 * d[0], 148 + 0.35 * d[1], 600.0)


SCATTERED = [
    (*corner_view(0), ["c0", "case"]),
    (*corner_view(1), ["c1", "case", "window"]),
    (*corner_view(2), ["c2", "window", "door"]),
    (*corner_view(3), ["c3", "door"]),
]


def walk() -> list:
    """Sixteen frames a second apart, circling the sheet at 1.3 m and looking across it at the far corner."""
    poses = []
    for k in range(16):
        a = 2 * np.pi * k / 16
        centre = (105 + 1300 * np.cos(a), 148 + 1300 * np.sin(a), 1450.0)
        target = (105 - 900 * np.cos(a), 148 - 900 * np.sin(a), 300.0)
        ahead = min(
            range(4), key=lambda i: np.hypot(*(np.array(CORNERS[i]) - [105 - 3000 * np.cos(a), 148 - 3000 * np.sin(a)]))
        )
        poses.append((centre, target, [f"c{ahead}"] + (["window"] if k in (0, 15) else [])))
    return poses


def check(measured_room: room.Room) -> None:
    """The room within 2% of the truth on every figure the brief asks for."""
    assert measured_room.floor_area_m2 == pytest.approx((X1 - X0) * (Y1 - Y0) / 1e6, rel=0.02)
    assert (measured_room.length_m, measured_room.width_m) == (
        pytest.approx(6.0, rel=0.02),
        pytest.approx(4.0, rel=0.02),
    )
    assert measured_room.height_m == pytest.approx(2.5, rel=0.02)


@pytest.mark.parametrize("headed", [1, -1, 0], ids=["compass", "anticlockwise compass", "shared features"])
def test_scattered_frames(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, headed: int) -> None:
    """Four far-apart frames align by compass, or by the door, window and bookcase they share."""
    measured_room = measured(monkeypatch, tmp_path, SCATTERED, headed)
    check(measured_room)
    assert measured_room.wall_area_m2 == pytest.approx(WALLS, rel=0.02)
    assert sorted((o.kind, o.width_m) for o in measured_room.openings) == [
        ("door", pytest.approx(0.9, rel=0.02)),
        ("window", pytest.approx(1.0, rel=0.02)),
    ]
    assert measured_room.shelved_wall_area_m2 == pytest.approx(3.6, rel=0.02)
    assert ("compass" in measured_room.scale_method) == bool(headed)


def test_slow_walk_aligns_by_camera_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Without a compass, frames that share no feature align because the camera barely moves between them."""
    check(measured(monkeypatch, tmp_path, walk(), headed=0))


def test_no_floor_sheet_leaves_the_room_blank(tmp_path: Path) -> None:
    """Without a floor sheet nothing is guessed."""
    frame = Frame(n=0, file="frames/00000.jpg", t=0, width=W, height=H)
    reading = Reading(frame=frame, spines=Spines(books=[]), scene=Scene(things=[]))
    blank = asyncio.run(room.measure(tmp_path, [reading]))
    assert blank.floor_area_m2 is None and "not measured" in blank.scale_method
