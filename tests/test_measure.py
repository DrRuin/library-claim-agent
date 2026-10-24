"""Spine sizes come only from known-size objects in the same frame, and a book never measures itself."""

import pytest

from app.identify import Identified, Identity
from app.measure import anchors, item_size, scale, sheet_scale, size
from app.merge import Group
from app.read import Frame, Reading, Scene, Sheet, Spine, Spines, Thing

FRAME = Frame(n=0, file="frames/00000.jpg", t=0, width=1000, height=1000)


def spine(box: list[int], pose: str = "upright", cut: bool = False) -> Spine:
    """A spine sighting with a box on the 0-1000 grid of a 1000 px frame."""
    return Spine(
        box_2d=box,
        row=1,
        unit="bookcase",
        pose=pose,
        readable="full",
        text="",
        title="",
        author="",
        publisher="",
        look="",
        cut_off=cut,
    )


def known(book_id: str, sighting: str, height: float | None) -> Identified:
    """A book identified with an optional catalogue height."""
    identity = Identity(
        verdict="identified",
        title=book_id,
        author="",
        publisher="",
        edition="",
        isbn="",
        confidence=0.9,
        evidence="",
        catalogue_url="",
        height_mm=height,
        thickness_mm=None,
        appraisal_reason="",
    )
    group = Group(
        id=book_id, unit="U1", shelf="S1", position=1, sightings=[sighting], best=sighting, notes=[], excluded_by=""
    )
    return Identified(group=group, identity=identity, flags=[])


def library(spines: list[Spine], sheet: Sheet | None = None) -> tuple[dict, dict]:
    """Readings and the sighting index for one frame."""
    reading = Reading(frame=FRAME, spines=Spines(books=spines, sheet=sheet), scene=Scene(things=[]))
    return {0: reading}, {f"0.b{i}": (reading, i) for i in range(len(spines))}


def test_a4_sheet_scale() -> None:
    """A 210 x 297 mm sheet seen as 210 x 297 px gives one millimetre a pixel."""
    sheet = Sheet(corners=[[100, 100], [100, 310], [397, 310], [397, 100]], surface="shelf")
    assert sheet_scale(sheet, FRAME) == pytest.approx(1.0)


def test_catalogue_heights_scale_other_books_but_not_themselves() -> None:
    """Two anchors agree; the third, unanchored book is measured from them; an anchor is measured from the others."""
    readings, where = library([spine([0, 0, 200, 30]), spine([0, 30, 200, 60]), spine([0, 60, 100, 80])])
    books = [known("B1", "0.b0", 200), known("B2", "0.b1", 220), known("B3", "0.b2", None)]
    found = anchors(readings, books, where)
    third = size(books[2], where, found)
    assert third.height_mm == pytest.approx(105)
    assert third.thickness_mm == pytest.approx(21)
    assert size(books[0], where, found).height_mm == pytest.approx(220)
    assert scale(found).loo_error == pytest.approx(0.0952, abs=1e-3)


def test_cut_off_and_unscaled_sightings_give_no_size() -> None:
    """A spine cut by the frame edge, or in a frame with no anchor, is left unmeasured."""
    readings, where = library([spine([0, 0, 200, 30], cut=True)])
    book = known("B1", "0.b0", None)
    assert size(book, where, anchors(readings, [book], where)).height_mm is None


def test_flat_books_measure_across() -> None:
    """A book lying flat has its spine length across the frame."""
    sheet = Sheet(corners=[[100, 100], [100, 310], [397, 310], [397, 100]], surface="shelf")
    readings, where = library([spine([500, 0, 530, 240], pose="flat")], sheet)
    book = known("B1", "0.b0", None)
    measured = size(book, where, anchors(readings, [book], where))
    assert (measured.height_mm, measured.thickness_mm) == (pytest.approx(240), pytest.approx(30))


def test_floor_sheet_is_not_a_spine_anchor() -> None:
    """A sheet on the floor is not in the plane of the spines."""
    sheet = Sheet(corners=[[100, 100], [100, 310], [397, 310], [397, 100]], surface="floor")
    readings, where = library([spine([0, 0, 200, 30])], sheet)
    assert anchors(readings, [known("B1", "0.b0", None)], where) == []


def test_items_measured_only_in_the_shelf_plane() -> None:
    """An ornament on the shelf takes the frame's scale; a chair in front of the bookcase does not."""
    sheet = Sheet(corners=[[100, 100], [100, 310], [397, 310], [397, 100]], surface="shelf")
    readings, where = library([], sheet)
    reading = readings[0]
    found = anchors(readings, [], where)

    def thing(plane: bool) -> Thing:
        """A 120 x 80 px object on the 1 mm/px frame."""
        return Thing(
            box_2d=[500, 500, 580, 620],
            category="decor",
            description="",
            material="",
            brand_model="",
            art=False,
            in_shelf_plane=plane,
            cut_off=False,
        )

    assert item_size([(reading, thing(True))], found)[:2] == (pytest.approx(120), pytest.approx(80))
    assert item_size([(reading, thing(False))], found)[:2] == (None, None)
