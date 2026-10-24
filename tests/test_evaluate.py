"""The eval script turns the judge's pairs into pass-bar numbers, and refuses a judgement that is not one to one."""

from pathlib import Path

import pytest

from app.config import Scoring
from app.evaluate import TruthBook, books, pair, room
from app.judge import BookPair, one_to_one
from app.packet import Book

RULES = Scoring()


def truth(shelf: str = "S1", n: int = 5) -> list[TruthBook]:
    """Legible books on one shelf."""
    return [
        TruthBook(book_id=f"{shelf}-{i}", unit="U1", shelf=shelf, position=i, legible_person="y", title=f"T{i}")
        for i in range(1, n + 1)
    ]


def line(i: int, identified: bool = True) -> Book:
    """A packet line."""
    return Book(
        id=f"b{i}",
        shelf="U1-S1",
        position=i,
        frame_ref="frames/0001.jpg",
        status="identified" if identified else "unidentified",
        title=f"T{i}" if identified else "",
    )


def judged(verdicts: dict[int, str]) -> list[BookPair]:
    """Judge answers pairing truth book i with line i."""
    return [BookPair(truth_id=f"S1-{i}", line_id=f"b{i}", verdict=v, reason="") for i, v in verdicts.items()]


def score(truth_books: list[TruthBook], lines: list[Book], pairs: list[BookPair]) -> dict[str, float | None]:
    """Bar values by name."""
    return {b.bar: b.value for b in books(pair(truth_books, lines, pairs), RULES)}


def test_all_same_work_scores_full_marks() -> None:
    """Every line judged the same work gives zero count error and no wrong lines."""
    result = score(truth(), [line(i) for i in range(1, 6)], judged(dict.fromkeys(range(1, 6), "same_work")))
    assert result == {
        "book count within 5%": 0,
        "titles identified (legible spines)": 1,
        "confidently wrong (identified lines)": 0,
    }


def test_different_work_is_confidently_wrong() -> None:
    """An identified line judged another work counts against the 3% bar."""
    verdicts = dict.fromkeys(range(1, 5), "same_work") | {5: "different_work"}
    result = score(truth(), [line(i) for i in range(1, 6)], judged(verdicts))
    assert result["confidently wrong (identified lines)"] == pytest.approx(1 / 5)
    assert result["titles identified (legible spines)"] == pytest.approx(4 / 5)


def test_unpaired_identified_line_is_wrong_and_extra() -> None:
    """A line the judge pairs with nothing adds to the count and, if identified, to the wrong lines."""
    lines = [line(i) for i in range(1, 7)]
    result = score(truth(), lines, judged(dict.fromkeys(range(1, 6), "same_work")))
    assert result["book count within 5%"] == pytest.approx(0.2)
    assert result["confidently wrong (identified lines)"] == pytest.approx(1 / 6)


def test_unidentified_lines_count_but_are_never_wrong() -> None:
    """Blank lines keep the count right and stay out of the wrong rate."""
    lines = [line(i, identified=False) for i in range(1, 6)]
    result = score(truth(), lines, judged(dict.fromkeys(range(1, 6), "no_title")))
    assert result["book count within 5%"] == 0
    assert result["titles identified (legible spines)"] == 0
    assert result["confidently wrong (identified lines)"] is None


def test_skipped_shelf_counts_against_the_packet() -> None:
    """A shelf the sweep never saw stays in every denominator."""
    result = score(
        truth("S1") + truth("S2"), [line(i) for i in range(1, 6)], judged(dict.fromkeys(range(1, 6), "same_work"))
    )
    assert result["book count within 5%"] == pytest.approx(-0.5)
    assert result["titles identified (legible spines)"] == pytest.approx(0.5)


def test_judge_answers_must_be_one_to_one() -> None:
    """A judgement that pairs one line twice or names an unknown id is refused."""
    twice = [BookPair(truth_id=t, line_id="b1", verdict="same_work", reason="") for t in ("S1-1", "S1-2")]
    with pytest.raises(ValueError, match="twice"):
        one_to_one(twice, {"S1-1", "S1-2"}, {"b1"})
    with pytest.raises(ValueError, match="unknown"):
        one_to_one(judged({9: "same_work"}), {"S1-1"}, {"b1"})


def test_l_shaped_room_area(tmp_path: Path) -> None:
    """An L-shaped floor and its walls come out of the tape readings."""
    (tmp_path / "corners.csv").write_text("x_mm,y_mm\n0,0\n4000,0\n4000,2000\n2000,2000\n2000,3000\n0,3000\n")
    (tmp_path / "room.csv").write_text("kind,value_mm\nceiling,2500\nceiling,2500\n")
    (tmp_path / "openings.csv").write_text("width_mm,height_mm\n900,2000\n")
    tape = room(tmp_path, RULES)
    assert tape.floor_area_m2 == pytest.approx(10.0)
    assert tape.wall_area_m2 == pytest.approx(14 * 2.5 - 1.8)
