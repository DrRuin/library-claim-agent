"""The judge model resolves each book's spine readings to a work, searching the web, and abstains when the spine does not support one."""

import asyncio
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from app.config import Locale, settings
from app.llm import fetch, image, reread, search
from app.merge import Group, Note
from app.price import PRICING, Offer
from app.read import Frame, Reading, Spine, crop

PROMPT = """<task>You identify one physical book on a policyholder's shelf for an insurance contents claim in {country} and price it in {currency}. You get crops of its spine from several frames and what a reader read on each. Search the web to resolve it to a specific work, then find its prices.</task>
<rules>
- identified only when the words on the spine support the work and a catalogue or retailer page the search found confirms that title and author. A spine with only an author, only a series name or only a publisher is unidentified.
- Never fill a title from what is likely on a shelf like this one. A blank costs far less than a wrong title.
- edition and publisher only when the spine shows the imprint, format or edition; isbn only when exactly one edition fits everything the spine shows.
- needs_appraisal when the book looks rare, antiquarian (printed before 1900), signed, a first or limited edition, or a policyholder remark says so. Give the reason.
- confidence: your probability that the title and author are right.
- evidence: one line on what the spine shows and what the page confirmed.
- catalogue_url: the search result that confirmed the work, exactly as the search gave it, preferably one that also prints this edition's dimensions; never a homepage or an address you composed.
- new and used: only for an identified book; otherwise found=false with every other field empty.
{pricing}
</rules>"""

CONFIRM = """<task>Check a book identification against this page for an insurance claim. The page text follows when it could be fetched; otherwise open the page.</task>
<page>{url}</page>
<claim>{title} by {author}{edition}</claim>
<rules>
- opened: whether you could read the page at all.
- confirms: the page shows a book with this title by this author, editor or compiler, in any edition or as a volume of a set. A list of search results confirms only when this title is one of its entries.
- height_mm and thickness_mm: the dimensions the page prints for the edition it shows, converted to millimetres; empty when it prints none. Height is the spine's long side, thickness its short side.
- reason: one line on what the page shows.
</rules>"""


class Identity(BaseModel):
    """What the judge concluded about one book."""

    verdict: Literal["identified", "unidentified", "needs_appraisal"]
    title: str
    author: str
    publisher: str
    edition: str
    isbn: str
    confidence: float = Field(ge=0, le=1)
    evidence: str
    catalogue_url: str
    appraisal_reason: str
    height_mm: float | None = None
    thickness_mm: float | None = None
    new: Offer | None = None
    used: Offer | None = None


class Confirmed(BaseModel):
    """What the checker read on the confirming page."""

    opened: bool
    confirms: bool
    height_mm: float | None
    thickness_mm: float | None
    reason: str


class Identified(BaseModel):
    """A book's identity with the checks code ran on its evidence."""

    group: Group
    identity: Identity
    flags: list[str]
    grounded: list[str] = []


def sightings(group: Group, where: dict[str, tuple[Reading, int]]) -> list[tuple[Frame, Spine]]:
    """The frame and spine of each sighting in a group."""
    return [(where[s][0].frame, where[s][0].spines.books[where[s][1]]) for s in group.sightings]


def blank(group: Group, reason: str) -> Identified:
    """An unidentified book that needed no search."""
    identity = Identity(
        verdict="unidentified",
        title="",
        author="",
        publisher="",
        edition="",
        isbn="",
        confidence=0,
        evidence=reason,
        catalogue_url="",
        appraisal_reason="",
    )
    return Identified(group=group, identity=identity, flags=[reason])


async def identify(
    run: Path, group: Group, where: dict[str, tuple[Reading, int]], notes: list[Note], locale: Locale
) -> Identified:
    """Resolve and price one book in one search, or leave it unidentified when no sighting has readable text."""
    seen = sightings(group, where)
    if all(spine.readable == "none" for _, spine in seen):
        return blank(group, "no sighting of the spine is readable")
    best = where[group.best][0].frame.n
    clear = {"full": 0, "partial": 1, "none": 2}
    seen.sort(key=lambda fs: (fs[0].n != best, clear[fs[1].readable], fs[1].cut_off))
    parts: list = []
    for frame, spine in seen[: settings().identify_crops]:
        parts += [
            f"Frame {frame.n}, reader saw: {spine.model_dump_json(exclude={'box_2d'})}",
            image(crop(run, frame, spine.box_2d)),
        ]
    remarks = [n.model_dump() for n in notes if n.id in group.notes]
    pricing = PRICING.format(country=locale.country)
    prompt = PROMPT.format(country=locale.country, currency=locale.currency, pricing=pricing)
    parts += [prompt, json.dumps({"policyholder_remarks": remarks}, ensure_ascii=False)]
    identity, pages = await search(Identity, parts, settings().judge_model)
    if identity.verdict == "unidentified":
        identity.title = identity.author = identity.edition = identity.isbn = ""
        identity.new = identity.used = None
        return Identified(group=group, identity=identity, flags=[])
    found = await confirmed(group, identity, [p.url for p in pages])
    found.grounded = [p.url for p in pages]
    return found


async def check(url: str, identity: Identity) -> tuple[str, Confirmed]:
    """A second model reads one candidate page against the identification."""
    source = await fetch(url)
    edition = f", {identity.edition}" if identity.edition else ""
    prompt = CONFIRM.format(url=source.url, title=identity.title, author=identity.author, edition=edition)
    try:
        return source.url, await reread(Confirmed, prompt, source)
    except TimeoutError:
        return source.url, Confirmed(
            opened=False, confirms=False, height_mm=None, thickness_mm=None, reason="the check timed out"
        )


async def confirmed(group: Group, identity: Identity, grounded: list[str]) -> Identified:
    """A second model reads the named page, then every page the search grounded on when it does not confirm; the first that confirms is kept."""
    if not identity.catalogue_url:
        return Identified(group=group, identity=identity, flags=["no confirming page was given"])
    named = await check(identity.catalogue_url, identity)
    others = [url for url in dict.fromkeys(grounded) if url != identity.catalogue_url]
    checks = [named] + ([] if named[1].confirms else await asyncio.gather(*(check(url, identity) for url in others)))
    if confirming := [(url, page) for url, page in checks if page.confirms]:
        identity.catalogue_url, page = confirming[0]
        identity.height_mm, identity.thickness_mm = page.height_mm, page.thickness_mm
        return Identified(group=group, identity=identity, flags=[])
    if not named[1].opened:
        return Identified(
            group=group, identity=identity, flags=[f"the checker could not open the confirming page: {named[1].reason}"]
        )
    rejected = identity.model_copy(update={"verdict": "unidentified", "confidence": 0})
    reasons = "; ".join(page.reason for _, page in checks if page.opened)
    rejected.evidence = f"{identity.evidence}; but no page the search found shows {identity.title}: {reasons}"
    rejected.title = rejected.author = rejected.edition = rejected.isbn = ""
    rejected.new = rejected.used = None
    return Identified(group=group, identity=rejected, flags=["no page the search found shows this work"])
