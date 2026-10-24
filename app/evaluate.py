"""Score a claim packet against the hand-collected ground truth, one row per pass bar."""

import argparse
import asyncio
import csv
from math import comb, sqrt
from pathlib import Path
from statistics import NormalDist
from typing import Literal

import numpy as np
from openpyxl import Workbook
from pydantic import BaseModel

from app import judge
from app.config import Scoring, settings
from app.judge import BookPair, ItemPair
from app.packet import Book, Excluded, Packet


class TruthBook(BaseModel):
    """One physical book as the collector recorded it."""

    book_id: str
    unit: str
    shelf: str
    position: int
    is_book: bool = True
    legible_person: Literal["y", "partial", "n"]
    title: str = ""
    part_number: str = ""
    author: str = ""
    thickness_mm: float | None = None


class TruthSpine(BaseModel):
    """A hand-measured spine."""

    book_id: str
    height_mm: float
    thickness_mm: float


class TruthPrice(BaseModel):
    """A hand-checked price of the same source type the packet cites."""

    book_id: str
    figure: Literal["replacement", "used"]
    price: float
    currency: str


class TruthItem(BaseModel):
    """One non-book object the collector listed."""

    item_id: str
    category: str
    description: str = ""
    location: str = ""


class Corner(BaseModel):
    """A floor corner in millimetres, walking the walls in order."""

    x_mm: float
    y_mm: float


class Measure(BaseModel):
    """One tape or laser reading from room.csv."""

    kind: Literal["wall", "diagonal", "ceiling", "recess"]
    value_mm: float


class Opening(BaseModel):
    """A door, window or arch in a wall."""

    width_mm: float
    height_mm: float


class TruthRoom(BaseModel):
    """Tape-measured room areas in square metres."""

    floor_area_m2: float
    wall_area_m2: float


def room(truth: Path, rules: Scoring) -> TruthRoom:
    """Shoelace floor area over the corners; net wall area is perimeter by mean ceiling height less openings."""
    corners = [Corner(**r) for r in rows(truth / "corners.csv")]
    xs, ys = np.array([c.x_mm for c in corners]), np.array([c.y_mm for c in corners])
    floor = abs(np.dot(xs, np.roll(ys, -1)) - np.dot(ys, np.roll(xs, -1))) / 2 / 1e6
    perimeter = np.hypot(np.diff(xs, append=xs[0]), np.diff(ys, append=ys[0])).sum() / 1e3
    ceilings = [m.value_mm for m in (Measure(**r) for r in rows(truth / "room.csv")) if m.kind == "ceiling"]
    openings = [Opening(**r) for r in rows(truth / "openings.csv")] if (truth / "openings.csv").exists() else []
    cut = sum(a for o in openings if (a := o.width_mm * o.height_mm / 1e6) >= rules.opening_min_m2)
    return TruthRoom(floor_area_m2=floor, wall_area_m2=perimeter * np.mean(ceilings) / 1e3 - cut)


class Bar(BaseModel):
    """One pass bar's result."""

    bar: str
    value: float | None
    numerator: float | None = None
    denominator: float | None = None
    ci_low: float | None = None
    ci_high: float | None = None
    threshold: float
    passed: bool | None
    notes: str = ""


class Pair(BaseModel):
    """A truth book and the packet line matched to it, or either alone."""

    truth: TruthBook | None
    line: Book | None
    verdict: str
    reason: str
    outcome: str


def rows(path: Path) -> list[dict[str, str]]:
    """Read a CSV into dicts, dropping empty cells so model defaults apply."""
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [{k: v for k, v in row.items() if v not in ("", None)} for row in csv.DictReader(f)]


def outcome(truth: TruthBook | None, line: Book | None, verdict: str) -> str:
    """Name what happened to a truth book or a packet line."""
    if verdict == "excluded":
        return "excluded"
    if line is None:
        return "missed"
    if truth is None:
        return "wrong" if line.status == "identified" else "extra"
    if line.status != "identified":
        return "queue" if line.status == "needs_appraisal" else "unidentified"
    return "right" if verdict == "same_work" else "wrong"


def pair(truth: list[TruthBook], lines: list[Book], judged: list[BookPair]) -> list[Pair]:
    """Join the judge's pairs with the unpaired books and lines on both sides; a pair on an excluded line is excluded."""
    books = {t.book_id: t for t in truth if t.is_book}
    by_id = {line.id: line for line in lines}
    found = [
        (books[j.truth_id], by_id.get(j.line_id), j.verdict if j.line_id in by_id else "excluded", j.reason)
        for j in judged
    ]
    found += [(t, None, "", "") for i, t in books.items() if i not in {j.truth_id for j in judged}]
    found += [(None, ln, "", "") for i, ln in by_id.items() if i not in {j.line_id for j in judged}]
    return [Pair(truth=t, line=ln, verdict=v, reason=r, outcome=outcome(t, ln, v)) for t, ln, v, r in found]


async def match(truth: list[TruthBook], lines: list[Book], excluded: list[Excluded]) -> list[Pair]:
    """Have the judge pair truth books with packet lines and the books the policyholder excluded, then name each outcome."""
    shown = [t.model_dump(exclude_none=True) for t in truth if t.is_book]
    left_out = [
        {"id": e.ref_id, "status": "excluded_by_user"} | e.model_dump(exclude={"ref_id", "said"})
        for e in excluded
        if e.shelf is not None
    ]
    judged = await judge.books(shown, [line.model_dump(mode="json", exclude_none=True) for line in lines] + left_out)
    return pair(truth, lines, judged)


def wilson(hits: int, n: int, confidence: float) -> tuple[float, float]:
    """Two-sided Wilson score interval for a proportion."""
    z = NormalDist().inv_cdf(1 - (1 - confidence) / 2)
    p, spread = hits / n, z * z / n
    centre = (p + spread / 2) / (1 + spread)
    half = z * sqrt(p * (1 - p) / n + spread / (4 * n)) / (1 + spread)
    return max(0.0, centre - half), min(1.0, centre + half)


def upper_bound(hits: int, n: int, confidence: float) -> float:
    """Exact one-sided Clopper-Pearson upper bound: the rate at which `hits` or fewer is as unlikely as 1 - confidence."""
    if hits == n:
        return 1.0
    low, high = hits / n, 1.0
    for _ in range(60):
        mid = (low + high) / 2
        tail = sum(comb(n, i) * mid**i * (1 - mid) ** (n - i) for i in range(hits + 1))
        low, high = (mid, high) if tail > 1 - confidence else (low, mid)
    return high


def proportion(name: str, hits: int, n: int, threshold: float, rules: Scoring, upper: bool = False) -> Bar:
    """A proportion bar with a Wilson interval; `upper` bars pass at or below the threshold."""
    if n == 0:
        return Bar(bar=name, value=None, threshold=threshold, passed=None, notes="no lines to score")
    low, high = wilson(hits, n, rules.confidence)
    value = hits / n
    notes = ""
    if upper:
        bound = upper_bound(hits, n, rules.confidence)
        notes = f"one-sided Clopper-Pearson upper bound {bound:.3f}"
    passed = value <= threshold if upper else value >= threshold
    return Bar(
        bar=name,
        value=value,
        numerator=hits,
        denominator=n,
        ci_low=low,
        ci_high=high,
        threshold=threshold,
        passed=passed,
        notes=notes,
    )


def within(name: str, errors: list[float | None], tolerance: float) -> Bar:
    """A sample bar: every sampled line must be within tolerance; a missing figure is a miss."""
    hits = sum(e is not None and e <= tolerance for e in errors)
    known = sorted(e for e in errors if e is not None)
    median = f"median error {known[len(known) // 2]:.3f}" if known else "no figures"
    return Bar(
        bar=name,
        value=hits / len(errors) if errors else None,
        numerator=hits,
        denominator=len(errors),
        threshold=tolerance,
        passed=bool(errors) and hits == len(errors),
        notes=median,
    )


def relative(pred: float | None, true: float) -> float | None:
    """Relative error, empty when there is no prediction."""
    return None if pred is None else abs(pred - true) / true


def books(pairs: list[Pair], rules: Scoring) -> list[Bar]:
    """Count, identification and confidently-wrong bars."""
    n_true = sum(p.truth is not None for p in pairs)
    n_pred = sum(p.line is not None or p.outcome == "excluded" for p in pairs)
    error = (n_pred - n_true) / n_true if n_true else None
    count = Bar(
        bar="book count within 5%",
        value=error,
        numerator=n_pred,
        denominator=n_true,
        threshold=rules.count_tolerance,
        passed=None if error is None else abs(error) <= rules.count_tolerance,
        notes=f"missed {sum(p.outcome == 'missed' for p in pairs)}, extra lines {sum(p.line is not None and p.truth is None for p in pairs)}",
    )
    legible = [p for p in pairs if p.truth is not None and p.truth.legible_person == "y" and p.outcome != "excluded"]
    right = sum(p.outcome == "right" for p in legible)
    identified = [p for p in pairs if p.line is not None and p.line.status == "identified"]
    wrong = sum(p.outcome == "wrong" for p in identified)
    return [
        count,
        proportion("titles identified (legible spines)", right, len(legible), rules.identified_bar, rules),
        proportion("confidently wrong (identified lines)", wrong, len(identified), rules.wrong_bar, rules, upper=True),
    ]


def spines(pairs: list[Pair], truth: list[TruthSpine], rules: Scoring) -> list[Bar]:
    """Height and thickness bars on the hand-measured sample."""
    lines = {p.truth.book_id: p.line for p in pairs if p.truth is not None}
    height, thick = [], []
    for s in truth:
        line = lines.get(s.book_id)
        height.append(relative(line and line.spine_height_cm and line.spine_height_cm * 10, s.height_mm))
        thick.append(relative(line and line.spine_thickness_cm and line.spine_thickness_cm * 10, s.thickness_mm))
    return [
        within("spine height within 15%", height, rules.spine_tolerance),
        within("spine thickness within 15%", thick, rules.spine_tolerance),
    ]


def prices(pairs: list[Pair], truth: list[TruthPrice], currency: str, rules: Scoring) -> Bar:
    """Book price bar on the hand-checked sample, in the packet's currency."""
    lines = {p.truth.book_id: p.line for p in pairs if p.truth is not None}
    errors = []
    for t in truth:
        if t.currency != currency:
            raise ValueError(f"price for {t.book_id} is in {t.currency}, the packet is in {currency}")
        line = lines.get(t.book_id)
        amount = (
            None if line is None else (line.replacement_cost if t.figure == "replacement" else line.used_value).amount
        )
        errors.append(relative(amount, t.price))
    return within("book prices within 25%", errors, rules.price_tolerance)


def items(packet: Packet, truth: list[TruthItem], judged: list[ItemPair], rules: Scoring) -> Bar:
    """Item recall with the right category, over the judge's pairs."""
    found = sum(j.category_right for j in judged)
    bar = proportion("non-book items found with category", found, len(truth), rules.items_bar, rules)
    bar.notes = f"{len(packet.items) - len(judged)} packet items unpaired"
    return bar


def area(name: str, pred: float | None, true: float, tolerance: float) -> Bar:
    """A room area bar."""
    error = relative(pred, true)
    return Bar(
        bar=name,
        value=error,
        numerator=pred,
        denominator=true,
        threshold=tolerance,
        passed=None if error is None else error <= tolerance,
        notes="" if pred is not None else "not measured",
    )


def timing(packet: Packet, rules: Scoring) -> Bar:
    """Seconds from sweep end to packet."""
    if packet.sweep_ended_at is None or packet.packet_at is None:
        return Bar(bar="time to packet", value=None, threshold=rules.packet_seconds, passed=None, notes="not recorded")
    seconds = (packet.packet_at - packet.sweep_ended_at).total_seconds()
    return Bar(
        bar="time to packet", value=seconds, threshold=rules.packet_seconds, passed=seconds < rules.packet_seconds
    )


async def score(packet: Packet, truth: Path, rules: Scoring) -> tuple[list[Bar], list[Pair]]:
    """Every bar for one packet against one truth folder."""
    pairs = await match([TruthBook(**r) for r in rows(truth / "books.csv")], packet.books, packet.excluded_by_user)
    bars = books(pairs, rules)
    if (truth / "spines.csv").exists():
        bars += spines(pairs, [TruthSpine(**r) for r in rows(truth / "spines.csv")], rules)
    if (truth / "prices.csv").exists():
        bars.append(prices(pairs, [TruthPrice(**r) for r in rows(truth / "prices.csv")], packet.sweep.currency, rules))
    if (truth / "items.csv").exists():
        listed = [TruthItem(**r) for r in rows(truth / "items.csv")]
        shown = [i.model_dump(mode="json", exclude_none=True) for i in packet.items]
        judged = await judge.items([i.model_dump() for i in listed], shown)
        bars.append(items(packet, listed, judged, rules))
    if (truth / "corners.csv").exists():
        tape = room(truth, rules)
        bars.append(area("floor area within 10%", packet.room.floor_area_m2, tape.floor_area_m2, rules.floor_tolerance))
        bars.append(area("wall area within 15%", packet.room.wall_area_m2, tape.wall_area_m2, rules.wall_tolerance))
    bars.append(timing(packet, rules))
    return bars, pairs


def write(out: Path, bars: list[Bar], pairs: list[Pair]) -> None:
    """Write the summary and per-book sheets as XLSX with CSV copies."""
    out.mkdir(parents=True, exist_ok=True)
    sheets = {
        "summary": [b.model_dump() for b in bars],
        "books": [
            {
                "truth_id": p.truth and p.truth.book_id,
                "line_id": p.line and p.line.id,
                "truth_shelf": p.truth and f"{p.truth.unit}-{p.truth.shelf}",
                "line_shelf": p.line and p.line.shelf,
                "legible": p.truth and p.truth.legible_person,
                "status": p.line and p.line.status,
                "truth_title": p.truth and p.truth.title,
                "pred_title": p.line and p.line.title,
                "verdict": p.verdict,
                "reason": p.reason,
                "outcome": p.outcome,
            }
            for p in pairs
        ],
    }
    book = Workbook()
    book.remove(book.active)
    for name, table in sheets.items():
        sheet = book.create_sheet(name)
        if not table:
            continue
        sheet.append(list(table[0]))
        for row in table:
            sheet.append(list(row.values()))
        with (out / f"{name}.csv").open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=list(table[0]))
            writer.writeheader()
            writer.writerows(table)
    book.save(out / "results.xlsx")


def main() -> None:
    """Score one packet from the command line and print the summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--packet", type=Path, required=True)
    parser.add_argument("--truth", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    packet = Packet.model_validate_json(args.packet.read_text(encoding="utf-8"))
    bars, pairs = asyncio.run(score(packet, args.truth, settings().scoring))
    write(args.out, bars, pairs)
    for b in bars:
        verdict = {True: "pass", False: "FAIL", None: "n/a"}[b.passed]
        value = "n/a" if b.value is None else f"{b.value:.3f}"
        print(f"{b.bar:42} {value:>9}  {verdict:5} {b.notes}")


if __name__ == "__main__":
    main()
