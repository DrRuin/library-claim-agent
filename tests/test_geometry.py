"""A sheet of known size on the floor recovers floor positions, the focal length and heights from one synthetic camera."""

import numpy as np
import pytest

from app.geometry import extent, height, on_floor, polygon, sheet_view

W, H, F = 2160, 3840, 2900.0


def camera() -> tuple[np.ndarray, np.ndarray]:
    """A phone held 1.5 m up, 2.5 m back from the sheet, looking down at the floor and across the room."""
    centre = np.array([600.0, -2500.0, 1500.0])
    target = np.array([1200.0, 1500.0, 600.0])
    forward = (target - centre) / np.linalg.norm(target - centre)
    right = np.cross(forward, [0, 0, 1.0])
    right /= np.linalg.norm(right)
    down = np.cross(forward, right)
    r = np.vstack([right, down, forward])
    return r, -r @ centre


def project(point: tuple[float, float, float]) -> list[float]:
    """The [y, x] point on 0-1000 where a floor-frame point in millimetres lands."""
    r, t = camera()
    x, y, z = r @ np.array(point) + t
    u, v = F * x / z + W / 2, F * y / z + H / 2
    return [v / H * 1000, u / W * 1000]


SHEET = [(0.0, 0.0, 0.0), (210.0, 0.0, 0.0), (210.0, 297.0, 0.0), (0.0, 297.0, 0.0)]


def test_floor_points_and_focal_come_back() -> None:
    """Exact corners give back floor points far from the sheet and the focal length."""
    view = sheet_view([project(p) for p in SHEET], W, H, (210, 297))
    assert view.focal_px == pytest.approx(F, rel=0.001)
    x, y = on_floor(project((-1500.0, 3000.0, 0.0)), view)
    assert (x, y) == (pytest.approx(-1500, rel=0.001), pytest.approx(3000, rel=0.001))


def test_sheet_turned_sideways_still_fits() -> None:
    """Corners listed starting on the long side give the same floor."""
    corners = [project(p) for p in SHEET]
    view = sheet_view(corners[1:] + corners[:1], W, H, (210, 297))
    a = on_floor(project((2000.0, 2500.0, 0.0)), view)
    b = on_floor(project((0.0, 0.0, 0.0)), view)
    assert np.hypot(a[0] - b[0], a[1] - b[1]) == pytest.approx(np.hypot(2000, 2500), rel=0.001)


def test_height_of_a_wall_corner() -> None:
    """A ceiling 2.7 m above a floor corner is measured."""
    view = sheet_view([project(p) for p in SHEET], W, H, (210, 297))
    foot, top = project((2500.0, 3500.0, 0.0)), project((2500.0, 3500.0, 2700.0))
    assert height(foot, top, view, view.focal_px) == pytest.approx(2700, rel=0.001)


def test_l_shaped_room() -> None:
    """Area, perimeter and the bounding size of an L-shaped floor."""
    corners = [(0, 0), (4000, 0), (4000, 2000), (2000, 2000), (2000, 3000), (0, 3000)]
    assert polygon(corners) == (pytest.approx(10e6), pytest.approx(14000))
    assert extent(corners) == (pytest.approx(4000), pytest.approx(3000))
