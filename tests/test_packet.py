"""Packet totals follow from the lines, and a packet that disagrees is refused."""

from datetime import datetime

import pytest
from pydantic import ValidationError

from app.config import Scoring
from app.evaluate import TruthItem, items
from app.judge import ItemPair
from app.packet import Book, Item, ItemPrice, Packet, Replacement, Room, Sweep, Used, assemble

SWEEP = Sweep(id="s1", captured_at=datetime(2026, 10, 21), country="IN", currency="INR")


def book(i: int, status: str = "identified", new: float | None = 500, used: float | None = 200) -> Book:
    """A book line with optional prices."""
    return Book(
        id=f"b{i}",
        shelf="U1-S1",
        position=i,
        frame_ref="frames/0001.jpg",
        status=status,
        spine_thickness_cm=2.5,
        replacement_cost=Replacement(amount=new),
        used_value=Used(amount=used),
    )


def item(i: str, category: str, status: str = "priced", low: float | None = 1000, high: float | None = 3000) -> Item:
    """An item line with an optional range."""
    return Item(
        id=i,
        category=category,
        frame_ref="frames/0002.jpg",
        status=status,
        replacement_cost=ItemPrice(low=low, high=high),
    )


def packet() -> Packet:
    """A packet with priced, unpriced and appraisal lines."""
    books = [book(1), book(2, new=None, used=None), book(3, "needs_appraisal"), book(4, "unidentified")]
    lines = [item("i1", "lighting"), item("i2", "art or print", "needs_appraisal"), item("i3", "furniture", "range")]
    return assemble(SWEEP, Room(), books, lines, [])


def test_totals_add_up_from_priced_lines_only() -> None:
    """Unpriced, appraisal and unidentified lines are excluded and counted as such."""
    totals = packet().totals
    assert (totals.book_count, totals.books_identified, totals.books_unidentified) == (4, 2, 1)
    assert (totals.books_replacement_cost, totals.books_used_value) == (500, 200)
    assert (totals.items_replacement_cost_low, totals.items_replacement_cost_high) == (2000, 6000)
    assert totals.excluded_from_totals == 4
    assert totals.shelf_run_m == pytest.approx(0.1)


def test_edited_totals_are_refused() -> None:
    """A packet whose totals were written by hand or by a model fails to load."""
    data = packet().model_dump(mode="json")
    data["totals"]["books_replacement_cost"] = 9999
    with pytest.raises(ValidationError, match="do not add up"):
        Packet.model_validate(data)


def test_missing_frames_are_reported(tmp_path) -> None:
    """Every frame reference must open a saved file."""
    (tmp_path / "frames").mkdir()
    (tmp_path / "frames" / "0001.jpg").write_bytes(b"")
    assert set(packet().missing_frames(tmp_path)) == {"frames/0002.jpg"}


def test_items_found_needs_the_right_category() -> None:
    """A paired item the judge says has the wrong category is not found."""
    truth = [TruthItem(item_id="t1", category="lighting"), TruthItem(item_id="t2", category="decor")]
    judged = [
        ItemPair(truth_id="t1", line_id="i1", category_right=True, reason=""),
        ItemPair(truth_id="t2", line_id="i3", category_right=False, reason=""),
    ]
    bar = items(packet(), truth, judged, Scoring())
    assert (bar.numerator, bar.denominator) == (1, 2)
