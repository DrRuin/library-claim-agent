"""The judge model pairs ground truth with packet lines; code only checks that its pairs are one to one."""

import json
from typing import Literal

from pydantic import BaseModel, Field

from app.config import settings
from app.llm import ask

BOOKS = """<task>You score an insurance inventory of a home library against the collector's ground truth.</task>
<rules>
- Pair each truth book with the one packet line that describes the same physical book, or leave it unpaired.
- Use the line's shelf, position, thickness and title together; packet shelf names may differ from the collector's.
- A line with status excluded_by_user is a book the policyholder left out of the claim; it has only a shelf, a position and a spine description. Pair it like any line, with verdict no_title.
- Each truth book and each packet line appears in at most one pair.
- same_work: the line names the same work as the truth book: the same title proper, the same part or volume, the same primary author. Subtitles, editions, translations and capitalisation do not matter.
- different_work: the line names another work, another volume, or only a series name.
- no_title: the line carries no title, so only its place on the shelf pairs it.
- Give a one-line reason for each pair.
</rules>"""

ITEMS = """<task>You score the non-book items of an insurance inventory against the collector's ground truth.</task>
<rules>
- Pair each truth item with the one packet item that describes the same physical object, or leave it unpaired.
- Each truth item and each packet item appears in at most one pair.
- category_right: the packet item's category means the same as the truth item's category.
- Give a one-line reason for each pair.
</rules>"""


class BookPair(BaseModel):
    """One pairing the judge made between a truth book and a packet line."""

    truth_id: str
    line_id: str
    verdict: Literal["same_work", "different_work", "no_title"]
    reason: str


class BookPairs(BaseModel):
    """Every book pairing the judge made."""

    pairs: list[BookPair]


class ItemPair(BaseModel):
    """One pairing the judge made between a truth item and a packet item."""

    truth_id: str
    line_id: str
    category_right: bool
    reason: str


class ItemPairs(BaseModel):
    """Every item pairing the judge made."""

    pairs: list[ItemPair] = Field(description="Pairs of the same physical object.")


def one_to_one[P: BookPair | ItemPair](pairs: list[P], truth: set[str], lines: set[str]) -> list[P]:
    """Refuse a judgement that names unknown ids or uses an id twice."""
    for side, known in (("truth_id", truth), ("line_id", lines)):
        ids = [getattr(p, side) for p in pairs]
        if unknown := set(ids) - known:
            raise ValueError(f"judge named unknown {side}s: {sorted(unknown)}")
        if len(ids) != len(set(ids)):
            raise ValueError(f"judge used a {side} twice")
    return pairs


async def books(truth: list[dict], lines: list[dict]) -> list[BookPair]:
    """Ask the judge to pair truth books with packet book lines."""
    data = json.dumps({"truth_books": truth, "packet_lines": lines}, ensure_ascii=False)
    answer = await ask(BookPairs, [BOOKS, data], settings().judge_model)
    return one_to_one(answer.pairs, {t["book_id"] for t in truth}, {line["id"] for line in lines})


async def items(truth: list[dict], lines: list[dict]) -> list[ItemPair]:
    """Ask the judge to pair truth items with packet items."""
    data = json.dumps({"truth_items": truth, "packet_items": lines}, ensure_ascii=False)
    answer = await ask(ItemPairs, [ITEMS, data], settings().judge_model)
    return one_to_one(answer.pairs, {t["item_id"] for t in truth}, {line["id"] for line in lines})
