"""Plane geometry from a sheet of known size on the floor: image to floor millimetres, the camera's pose, and heights above the floor."""

import numpy as np
from pydantic import BaseModel


class View(BaseModel):
    """One frame's floor homography and, when the focal length is known, the camera pose."""

    h: list[list[float]]
    focal_px: float | None
    width: int
    height: int


def homography(src: np.ndarray, dst: np.ndarray) -> np.ndarray:
    """The 3x3 map taking four or more src points to dst points (direct linear transform)."""
    rows = []
    for (x, y), (u, v) in zip(src, dst, strict=True):
        rows.append([-x, -y, -1, 0, 0, 0, u * x, u * y, u])
        rows.append([0, 0, 0, -x, -y, -1, v * x, v * y, v])
    h = np.linalg.svd(np.array(rows))[2][-1].reshape(3, 3)
    return h / h[2, 2]


def focal(h: np.ndarray) -> list[float]:
    """Focal length estimates in pixels from a floor-to-image homography in centred pixels (square pixels)."""
    (a, b, _), (c, d, _), (e, g, _) = h
    out = []
    if abs(e * g) > 1e-12 and (f2 := -(a * b + c * d) / (e * g)) > 0:
        out.append(float(np.sqrt(f2)))
    if abs(e * e - g * g) > 1e-12 and (f2 := -(a * a + c * c - b * b - d * d) / (e * e - g * g)) > 0:
        out.append(float(np.sqrt(f2)))
    return out


def sheet_view(corners: list[list[float]], width: int, height: int, sheet_mm: tuple[float, float]) -> View:
    """Floor homography from the sheet's corners as [y, x] on 0-1000, with x along a short side, found by focal agreement (by image side lengths when no focal fits)."""
    pixels = np.array([[x * width / 1000 - width / 2, y * height / 1000 - height / 2] for y, x in corners])
    w, l = sorted(sheet_mm)
    floor = np.array([[0, 0], [w, 0], [w, l], [0, l]], dtype=float)
    sides = np.linalg.norm(pixels - np.roll(pixels, -1, axis=0), axis=1)
    short = sides[0] + sides[2] > sides[1] + sides[3]
    fits = []
    for start in (0, 1):
        to_image = homography(floor, np.roll(pixels, -start, axis=0))
        guesses = focal(to_image)
        spread = abs(guesses[0] - guesses[1]) / max(guesses) if len(guesses) == 2 else np.inf
        fits.append((spread, start != short, to_image, guesses))
    _, _, to_image, guesses = min(fits, key=lambda f: (f[0], f[1]))
    return View(h=to_image.tolist(), focal_px=float(np.mean(guesses)) if guesses else None, width=width, height=height)


def centred(point: list[float], view: View) -> np.ndarray:
    """A [y, x] point on 0-1000 as centred pixel coordinates."""
    y, x = point
    return np.array([x * view.width / 1000 - view.width / 2, y * view.height / 1000 - view.height / 2])


def on_floor(point: list[float], view: View) -> tuple[float, float]:
    """Where an image point on the floor lies, in millimetres in the sheet's frame."""
    u, v = centred(point, view)
    x, y, w = np.linalg.inv(np.array(view.h)) @ np.array([u, v, 1.0])
    return float(x / w), float(y / w)


def pose(view: View, focal_px: float) -> tuple[np.ndarray, np.ndarray]:
    """Camera rotation and translation (floor millimetres to camera) from the homography and a focal length."""
    k_inv = np.diag([1 / focal_px, 1 / focal_px, 1.0])
    b = k_inv @ np.array(view.h)
    scale = 2 / (np.linalg.norm(b[:, 0]) + np.linalg.norm(b[:, 1]))
    if (b[:, 2] * scale)[2] < 0:
        scale = -scale
    r1, r2, t = b[:, 0] * scale, b[:, 1] * scale, b[:, 2] * scale
    r = np.column_stack([r1, r2, np.cross(r1, r2)])
    u, _, vt = np.linalg.svd(r)
    return u @ vt, t


def height(foot: list[float], top: list[float], view: View, focal_px: float) -> float:
    """Height in millimetres of an image point straight above a floor point, by least squares on the projection."""
    r, t = pose(view, focal_px)
    x, y = on_floor(foot, view)
    u, v = centred(top, view) / focal_px
    base = r @ np.array([x, y, 0.0]) + t
    up = r[:, 2]
    a = np.array([u * up[2] - up[0], v * up[2] - up[1]])
    b = np.array([base[0] - u * base[2], base[1] - v * base[2]])
    z = float(a @ b / (a @ a))
    camera_z = float(-(r.T @ t)[2])
    return z if camera_z > 0 else -z


def polygon(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Area and perimeter of a simple polygon given in order."""
    xs, ys = np.array([p[0] for p in points]), np.array([p[1] for p in points])
    area = abs(np.dot(xs, np.roll(ys, -1)) - np.dot(ys, np.roll(xs, -1))) / 2
    perimeter = float(np.hypot(np.diff(xs, append=xs[0]), np.diff(ys, append=ys[0])).sum())
    return float(area), perimeter


def extent(points: list[tuple[float, float]]) -> tuple[float, float]:
    """Length and width of the smallest rectangle around the polygon with a side along one of its edges."""
    pts = np.array(points)
    best = None
    for i in range(len(pts)):
        edge = pts[(i + 1) % len(pts)] - pts[i]
        axis = edge / np.linalg.norm(edge)
        normal = np.array([-axis[1], axis[0]])
        along, across = pts @ axis, pts @ normal
        size = (float(np.ptp(along)), float(np.ptp(across)))
        if best is None or size[0] * size[1] < best[0] * best[1]:
            best = size
    return max(best), min(best)


def anticlockwise(corners: list[list[float]]) -> list[list[float]]:
    """The sheet's [y, x] corners listed anticlockwise on screen, so every frame labels the sheet with the same handedness."""
    pts = [(x, -y) for y, x in corners]
    signed = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(pts, pts[1:] + pts[:1], strict=True))
    return corners if signed > 0 else corners[::-1]


def facing(view: View, focal_px: float) -> float:
    """Degrees anticlockwise, seen from above, from the sheet's x axis to where the camera looks."""
    r, t = pose(view, focal_px)
    up = 1.0 if float(-(r.T @ t)[2]) > 0 else -1.0
    return float(np.degrees(np.arctan2(r[2, 1], r[2, 0]))) * up


def position(view: View, focal_px: float) -> np.ndarray:
    """Where the camera stands on the floor, in millimetres in the sheet's frame."""
    r, t = pose(view, focal_px)
    return (-(r.T @ t))[:2]
