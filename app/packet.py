"""The claim packet contract, with totals and the review queue computed from its lines."""

from datetime import datetime
from pathlib import Path
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

M2_TO_FT2 = 10.763910416709722


class Sweep(BaseModel):
    """One continuous capture of one room."""

    id: str
    captured_at: datetime
    device: str = ""
    duration_s: float = 0
    country: str
    currency: str
    frames: int = 0


class Opening(BaseModel):
    """A door, window or arch measured in the sweep."""

    kind: str
    width_m: float
    height_m: float
    frame_ref: str


class Room(BaseModel):
    """Room geometry; a field stays empty when the sweep could not measure it."""

    length_m: float | None = None
    width_m: float | None = None
    height_m: float | None = None
    floor_area_m2: float | None = None
    floor_area_ft2: float | None = None
    wall_area_m2: float | None = None
    wall_area_ft2: float | None = None
    gross_wall_area_m2: float | None = None
    shelved_wall_area_m2: float | None = None
    shelved_wall_area_ft2: float | None = None
    shape: str = ""
    corners_m: list[tuple[float, float]] = Field(default_factory=list)
    openings: list[Opening] = Field(default_factory=list)
    scale_method: str = ""
    confidence: float | None = None
    frame_refs: list[str] = Field(default_factory=list)


class Replacement(BaseModel):
    """A book's new or equivalent price today, with where it came from."""

    amount: float | None = None
    source: str = ""
    url: str = ""
    retrieved_at: datetime | None = None
    converted: bool = False
    original_amount: float | None = None
    original_currency: str = ""
    quote: str = ""
    condition_assumed: str = ""
    notes: list[str] = []


class Used(BaseModel):
    """A book's second-hand market value, with where it came from."""

    amount: float | None = None
    source: str = ""
    url: str = ""
    retrieved_at: datetime | None = None
    condition_assumed: str = ""
    converted: bool = False
    original_amount: float | None = None
    original_currency: str = ""
    quote: str = ""
    notes: list[str] = []


class Book(BaseModel):
    """One physical book seen on a shelf."""

    id: str
    shelf: str
    position: int
    frame_ref: str
    status: Literal["identified", "unidentified", "needs_appraisal"]
    title: str = ""
    author: str = ""
    edition: str = ""
    isbn: str = ""
    spine_height_cm: float | None = None
    spine_thickness_cm: float | None = None
    id_confidence: float | None = None
    replacement_cost: Replacement = Replacement()
    used_value: Used = Used()
    publisher: str = ""
    spine_text: str = ""
    lying: str = ""
    frame_refs: list[str] = Field(default_factory=list)
    box_2d: list[int] = Field(default_factory=list)
    size_method: str = ""
    evidence: str = ""
    user_notes: list[str] = Field(default_factory=list)
    crop_ref: str = ""


class Dimensions(BaseModel):
    """Approximate size of an item in centimetres; depth stays empty, as a frame shows only the front."""

    w: float | None = None
    h: float | None = None
    d: float | None = None


class ItemPrice(BaseModel):
    """An item's local replacement cost as a sourced range."""

    low: float | None = None
    high: float | None = None
    source: str = ""
    url: str = ""
    retrieved_at: datetime | None = None
    converted: bool = False
    quote: str = ""
    condition_assumed: str = "new"


class Item(BaseModel):
    """One non-book object in the room."""

    id: str
    category: str
    description: str = ""
    brand_model: str = ""
    frame_ref: str
    dimensions_cm: Dimensions = Dimensions()
    size_method: str = ""
    status: Literal["priced", "range", "needs_appraisal"]
    replacement_cost: ItemPrice = ItemPrice()
    confidence: float | None = None
    material: str = ""
    frame_refs: list[str] = Field(default_factory=list)
    box_2d: list[int] = Field(default_factory=list)
    user_notes: list[str] = Field(default_factory=list)
    crop_ref: str = ""


class SecondMarket(BaseModel):
    """One book priced again in another market, to show the locale is a setting."""

    book_id: str
    country: str
    currency: str
    replacement_cost: Replacement = Replacement()
    used_value: Used = Used()


class Excluded(BaseModel):
    """A line the policyholder took out of the claim by voice, with what they said."""

    ref_id: str
    description: str
    said: str
    shelf: str | None = None
    position: int | None = None


class Totals(BaseModel):
    """Sums over the lines, computed in code."""

    book_count: int
    books_identified: int
    books_unidentified: int
    shelf_run_m: float
    books_replacement_cost: float
    books_used_value: float
    items_replacement_cost_low: float
    items_replacement_cost_high: float
    excluded_from_totals: int


class Review(BaseModel):
    """A line the system is not confident in, with the reason and what a person has to do about it."""

    ref_id: str
    reason: str
    action: Literal["identify", "price", "appraise", "measure", "system"] = "system"


class Stage(BaseModel):
    """One pipeline stage's run: wall time, calls, tokens and list-price cost."""

    name: str
    started_at: datetime
    finished_at: datetime
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    searches: int = 0
    usd: float = 0
    call_seconds: list[float] = Field(default_factory=list)


class Packet(BaseModel):
    """The claim packet for one sweep."""

    sweep: Sweep
    room: Room
    books: list[Book]
    items: list[Item]
    totals: Totals
    review_queue: list[Review]
    second_market: list[SecondMarket] = Field(default_factory=list)
    excluded_by_user: list[Excluded] = Field(default_factory=list)
    sweep_ended_at: datetime | None = None
    packet_at: datetime | None = None
    stages: list[Stage] = Field(default_factory=list)
    cost_usd: float = 0
    assumptions: list[str] = Field(default_factory=list)
    final: bool = True

    @model_validator(mode="after")
    def balanced(self) -> Self:
        """Refuse a packet whose totals do not follow from its lines."""
        if self.totals != total(self.books, self.items):
            raise ValueError("totals do not add up from the lines")
        return self

    def missing_frames(self, run: Path) -> list[str]:
        """Frame references that point to no saved file under the run folder."""
        refs = [ref for line in [*self.books, *self.items] for ref in [line.frame_ref, *line.frame_refs]]
        refs += self.room.frame_refs + [o.frame_ref for o in self.room.openings]
        return sorted({ref for ref in refs if not (run / ref).is_file()})


def priced(book: Book) -> bool:
    """Whether a book's figures count towards the totals."""
    return book.status == "identified" and book.replacement_cost.amount is not None


def counted(item: Item) -> bool:
    """Whether an item's range counts towards the totals."""
    return item.status != "needs_appraisal" and None not in (item.replacement_cost.low, item.replacement_cost.high)


def total(books: list[Book], items: list[Item]) -> Totals:
    """Add up the lines; unpriced and appraisal lines are counted as excluded."""
    sold = [b for b in books if priced(b)]
    kept = [i for i in items if counted(i)]
    return Totals(
        book_count=len(books),
        books_identified=sum(b.status == "identified" for b in books),
        books_unidentified=sum(b.status == "unidentified" for b in books),
        shelf_run_m=round(sum(b.spine_thickness_cm or 0 for b in books) / 100, 3),
        books_replacement_cost=round(sum(b.replacement_cost.amount for b in sold), 2),
        books_used_value=round(sum(b.used_value.amount or 0 for b in sold), 2),
        items_replacement_cost_low=round(sum(i.replacement_cost.low for i in kept), 2),
        items_replacement_cost_high=round(sum(i.replacement_cost.high for i in kept), 2),
        excluded_from_totals=len(books) - len(sold) + len(items) - len(kept),
    )


def assemble(
    sweep: Sweep, room: Room, books: list[Book], items: list[Item], review: list[Review], **rest: object
) -> Packet:
    """Build a packet, computing its totals and cost from the lines and stages."""
    stages = rest.pop("stages", [])
    return Packet(
        sweep=sweep,
        room=room,
        books=books,
        items=items,
        totals=total(books, items),
        review_queue=review,
        stages=stages,
        cost_usd=round(sum(s.usd for s in stages), 4),
        **rest,
    )
