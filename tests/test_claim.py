"""The packet keeps unknowns blank, sends doubtful lines to review, never prices appraisal lines and points at saved frames."""

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.claim import PENDING_PRICE, build, close
from app.config import Locale, settings
from app.identify import Identified, Identity
from app.merge import Group, Library, Note
from app.packet import Room
from app.price import Priced
from app.read import Frame, Reading, Scene, Spine, Spines
from app.sweep import BookPrice, Sweep

LOCALE = Locale(country="India", currency="INR", appraisal_limit=100_000)


def offer(amount: float) -> Priced:
    """A checked offer in rupees."""
    return Priced(
        amount=amount,
        source="shop",
        url="https://shop.example/b",
        retrieved_at=datetime.now(UTC),
        converted=False,
        original_amount=amount,
        original_currency="INR",
        quote=f"Rs {amount}",
        condition="new",
        flags=[],
    )


def identity(verdict: str, confidence: float = 0.95) -> Identity:
    """An identification result."""
    return Identity(
        verdict=verdict,
        title="A Book" if verdict != "unidentified" else "",
        author="An Author",
        publisher="",
        edition="",
        isbn="",
        confidence=confidence,
        evidence="spine reads A Book",
        catalogue_url="",
        height_mm=None,
        thickness_mm=None,
        appraisal_reason="signed by the author" if verdict == "needs_appraisal" else "",
    )


def sweep(tmp_path: Path) -> Sweep:
    """A finished sweep of one frame and five books, without any model call."""
    run = Sweep(tmp_path / "run", LOCALE)
    frame = Frame(n=0, file="frames/00000.jpg", t=0, width=1000, height=1000)
    (run.run / frame.file).write_bytes(b"jpeg")
    run.frames = [frame]
    spines = [
        Spine(
            box_2d=[0, i * 100, 300, i * 100 + 40],
            row=1,
            unit="case",
            pose="upright",
            readable="full",
            text=f"spine {i}",
            title="",
            author="",
            publisher="",
            look="",
            cut_off=False,
        )
        for i in range(5)
    ]
    run.readings = {0: Reading(frame=frame, spines=Spines(books=spines), scene=Scene(things=[]))}
    run.notes = [Note(id="R1", t=1, frame=0, kind="exclude", said="those are not mine", about="last book")]
    books = [
        Group(
            id=f"B{i + 1}",
            unit="U1",
            shelf="S1",
            position=i + 1,
            sightings=[f"0.b{i}"],
            best=f"0.b{i}",
            notes=[],
            excluded_by="R1" if i == 4 else "",
        )
        for i in range(5)
    ]
    run.library = Library(books=books, merged=[0])
    verdicts = [
        identity("identified"),
        identity("identified", 0.5),
        identity("unidentified"),
        identity("needs_appraisal"),
    ]
    run.identified = {b.id: Identified(group=b, identity=v, flags=[]) for b, v in zip(books, verdicts, strict=False)}
    run.prices = {
        ("B1", "INR"): BookPrice(book="B1", locale=LOCALE, new=offer(450), used=offer(150), gaps=[]),
        ("B2", "INR"): BookPrice(
            book="B2", locale=LOCALE, new=offer(250_000), used=None, gaps=["used: no price found"]
        ),
    }
    run.ended = datetime.now(UTC)
    return run


def test_packet_lines_and_totals(tmp_path: Path) -> None:
    """Only the confident, checked price counts; the expensive one is sent to appraisal unpriced."""
    packet = build(sweep(tmp_path), Room(scale_method="not measured in this test"))
    status = {b.id: b.status for b in packet.books}
    assert status == {"B1": "identified", "B2": "needs_appraisal", "B3": "unidentified", "B4": "needs_appraisal"}
    assert packet.books[1].replacement_cost.amount is None
    assert packet.books[2].title == "" and packet.books[2].id_confidence is None
    assert (packet.totals.book_count, packet.totals.books_replacement_cost, packet.totals.books_used_value) == (
        4,
        450,
        150,
    )
    assert packet.excluded_by_user[0].ref_id == "B5" and packet.excluded_by_user[0].said == "those are not mine"


def test_review_queue_explains_every_doubt(tmp_path: Path) -> None:
    """Appraisal, low confidence, missing sizes and the unmeasured room are all queued with a reason."""
    packet = build(sweep(tmp_path), Room(scale_method="not measured in this test"))
    reasons = {(r.ref_id, r.reason.split(":")[0]) for r in packet.review_queue}
    assert ("B2", "a price of 250,000 INR is at or above the appraisal limit") in reasons
    assert ("B4", "needs appraisal") in reasons
    assert ("room", "room not measured") in reasons
    assert any(r.ref_id == "B3" for r in packet.review_queue)


def test_every_frame_reference_opens(tmp_path: Path) -> None:
    """The packet's frame references all point at saved files."""
    run = sweep(tmp_path)
    assert build(run, Room()).missing_frames(run.run) == []


def test_packet_at_the_deadline_then_final(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Work still running when the packet falls due is listed for review, and the final packet follows with it done."""
    run = sweep(tmp_path)
    priced = run.prices.pop(("B1", "INR"))

    async def finish() -> None:
        """Pricing that lands after the deadline."""
        await asyncio.sleep(0.2)
        run.prices[("B1", "INR")] = priced

    async def packets() -> list:
        """Every packet the sweep writes."""
        return [p async for p in close(run)]

    monkeypatch.setattr(run, "finish", finish)
    monkeypatch.setattr(settings(), "packet_due_s", 0.05)
    early, final = asyncio.run(packets())
    assert (early.final, final.final) == (False, True)
    assert any(r.ref_id == "B1" and r.reason == PENDING_PRICE for r in early.review_queue)
    assert early.totals.books_replacement_cost == 0 and final.totals.books_replacement_cost == 450
