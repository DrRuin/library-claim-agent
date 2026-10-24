"""Turn a finished sweep into the claim packet: lines from the stage outputs, totals and the review queue in code."""

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime

from app import measure
from app.config import settings
from app.packet import (
    Book,
    Dimensions,
    Excluded,
    Item,
    ItemPrice,
    Packet,
    Replacement,
    Review,
    Room,
    SecondMarket,
    Sweep,
    Used,
    assemble,
)
from app.price import Priced
from app.read import crop
from app.report import render
from app.sweep import Sweep as Run

PENDING = "still being identified when this packet was written; the final packet follows"
PENDING_PRICE = "still being priced when this packet was written; the final packet follows"


def replacement(p: Priced | None) -> Replacement:
    """A packet replacement figure from a checked offer."""
    if p is None:
        return Replacement()
    return Replacement(
        amount=p.amount,
        source=p.source,
        url=p.url,
        retrieved_at=p.retrieved_at,
        converted=p.converted,
        original_amount=p.original_amount,
        original_currency=p.original_currency,
        quote=p.quote,
        condition_assumed=p.condition,
        notes=p.flags + [p.rate] * bool(p.rate),
    )


def used(p: Priced | None) -> Used:
    """A packet used-value figure from a checked offer."""
    if p is None:
        return Used()
    return Used(
        amount=p.amount,
        source=p.source,
        url=p.url,
        retrieved_at=p.retrieved_at,
        condition_assumed=p.condition,
        converted=p.converted,
        original_amount=p.original_amount,
        original_currency=p.original_currency,
        quote=p.quote,
        notes=p.flags + [p.rate] * bool(p.rate),
    )


def books(run: Run, review: list[Review], excluded: list[Excluded], final: bool) -> list[Book]:
    """One packet line per logged book, in shelf order, with its review reasons."""
    s = settings()
    where = run.where()
    identified = [i for i in run.identified.values() if i]
    anchors = measure.anchors(run.readings, identified, where)
    remarks = {n.id: n for n in run.notes}
    lines = []
    for g in sorted(run.library.books, key=lambda b: (b.unit, b.shelf, b.position)):
        reading, i = where[g.best]
        spine = reading.spines.books[i]
        if g.excluded_by:
            excluded.append(
                Excluded(
                    ref_id=g.id,
                    description=spine.text or spine.look,
                    said=remarks[g.excluded_by].said,
                    shelf=g.shelf,
                    position=g.position,
                )
            )
            continue
        found = run.identified.get(g.id)
        identity = found.identity if found else None
        status = identity.verdict if identity else "unidentified"
        size = (
            measure.size(found, where, anchors)
            if found
            else measure.Size(height_mm=None, thickness_mm=None, method="not identified yet")
        )
        offer = run.prices.get((g.id, run.locale.currency))
        new, old = (offer.new, offer.used) if offer and status == "identified" else (None, None)
        unknown = PENDING if not final else "the book was not identified"
        reasons = [("identify", f) for f in found.flags] if found else [("identify", unknown)]
        if status == "identified" and offer is None:
            reasons.append(("price", PENDING_PRICE if not final else "the book was not priced"))
        top = max([p.amount for p in (new, old) if p] or [0])
        if top >= run.locale.appraisal_limit:
            status, new, old = "needs_appraisal", None, None
            reasons.append(
                ("appraise", f"a price of {top:,.0f} {run.locale.currency} is at or above the appraisal limit")
            )
        if identity and status == "needs_appraisal" and identity.appraisal_reason:
            reasons.append(("appraise", f"needs appraisal: {identity.appraisal_reason}"))
        if status == "identified" and identity.confidence < s.review_confidence:
            reasons.append(("identify", f"identification confidence {identity.confidence:.2f}: {identity.evidence}"))
        if offer and status == "identified":
            reasons += [("price", gap) for gap in offer.gaps]
            figures = (("replacement", new), ("used", old))
            reasons += [("price", f"{kind}: {flag}") for kind, p in figures if p for flag in p.flags]
        if size.height_mm is None:
            reasons.append(("measure", f"no spine size: {size.method}"))
        review += [Review(ref_id=g.id, reason=r, action=a) for a, r in reasons]
        frames = sorted({where[x][0].frame.file for x in g.sightings})
        lines.append(
            Book(
                id=g.id,
                shelf=f"{g.unit}-{g.shelf}",
                position=g.position,
                frame_ref=reading.frame.file,
                status=status,
                title=identity.title if identity else "",
                author=identity.author if identity else "",
                edition=identity.edition if identity else "",
                isbn=identity.isbn if identity else "",
                publisher=identity.publisher if identity else spine.publisher,
                spine_height_cm=size.height_mm / 10 if size.height_mm else None,
                spine_thickness_cm=size.thickness_mm / 10 if size.thickness_mm else None,
                id_confidence=identity.confidence if identity and status != "unidentified" else None,
                replacement_cost=replacement(new),
                used_value=used(old),
                spine_text=spine.text,
                lying=spine.pose,
                frame_refs=frames,
                box_2d=spine.box_2d,
                size_method=size.method,
                evidence=identity.evidence if identity else "",
                user_notes=[remarks[n].said for n in g.notes],
            )
        )
    return lines


def items(run: Run, review: list[Review], excluded: list[Excluded], final: bool) -> list[Item]:
    """One packet line per object, with its review reasons."""
    where = run.where_things()
    anchors = measure.anchors(run.readings, [i for i in run.identified.values() if i], run.where())
    remarks = {n.id: n for n in run.notes}
    lines = []
    for obj in run.contents.objects:
        ref = obj.id
        reading, j = where[obj.best]
        thing = reading.scene.things[j]
        if obj.excluded_by:
            excluded.append(Excluded(ref_id=ref, description=obj.description, said=remarks[obj.excluded_by].said))
            continue
        priced = run.item_prices.get(obj.id)
        status = priced.status if priced else "range"
        offers = priced.offers if priced else []
        if status != "needs_appraisal" and offers and max(o.amount for o in offers) >= run.locale.appraisal_limit:
            status = "needs_appraisal"
            review.append(
                Review(ref_id=ref, reason="a comparable price is at or above the appraisal limit", action="appraise")
            )
        if status == "needs_appraisal":
            offers = []
            review.append(
                Review(
                    ref_id=ref,
                    reason=priced.basis
                    if priced and priced.basis
                    else "art or a high-value object needs a human appraisal",
                    action="appraise",
                )
            )
        elif not offers:
            review.append(
                Review(
                    ref_id=ref,
                    reason="no checked price: "
                    + "; ".join(priced.gaps if priced else [PENDING_PRICE if not final else "not priced"]),
                    action="price",
                )
            )
        review += [Review(ref_id=ref, reason=f"{o.source}: {flag}", action="price") for o in offers for flag in o.flags]
        w, h, method = measure.item_size(
            [(where[x][0], where[x][0].scene.things[where[x][1]]) for x in obj.sightings], anchors
        )
        cheap = min(offers, key=lambda o: o.amount) if offers else None
        dear = max(offers, key=lambda o: o.amount) if offers else None
        lines.append(
            Item(
                id=ref,
                category=obj.category,
                description=obj.description,
                brand_model=obj.brand_model,
                frame_ref=reading.frame.file,
                dimensions_cm=Dimensions(w=w and w / 10, h=h and h / 10),
                size_method=method,
                status=status if offers or status == "needs_appraisal" else "range",
                replacement_cost=ItemPrice(
                    low=cheap.amount if cheap else None,
                    high=dear.amount if dear else None,
                    source="; ".join(sorted({o.source for o in offers})),
                    url=" ".join(sorted({o.url for o in offers})),
                    retrieved_at=cheap.retrieved_at if cheap else None,
                    converted=any(o.converted for o in offers),
                    quote="; ".join(o.quote for o in offers),
                ),
                material=thing.material,
                frame_refs=sorted({where[x][0].frame.file for x in obj.sightings}),
                box_2d=thing.box_2d,
                user_notes=[remarks[n].said for n in obj.notes],
            )
        )
    return lines


def second_market(run: Run) -> list[SecondMarket]:
    """The sample of books priced again in the second market."""
    other = settings().second_locale
    return [
        SecondMarket(
            book_id=p.book,
            country=other.country,
            currency=other.currency,
            replacement_cost=replacement(p.new),
            used_value=used(p.used),
        )
        for (book, currency), p in run.prices.items()
        if currency == other.currency
    ]


def build(run: Run, room: Room, final: bool = True) -> Packet:
    """The claim packet of a sweep; a packet that is not final lists the lines still being worked on for review."""
    review: list[Review] = []
    excluded: list[Excluded] = []
    lines = books(run, review, excluded, final)
    things = items(run, review, excluded, final)
    review += [Review(ref_id="pipeline", reason=e) for e in run.errors]
    if room.floor_area_m2 is None:
        review.append(Review(ref_id="room", reason=f"room not measured: {room.scale_method}", action="measure"))
    ended = run.ended or datetime.now(UTC)
    sweep = Sweep(
        id=run.run.name,
        captured_at=run.started,
        device=run.device,
        duration_s=round((ended - run.started).total_seconds(), 1),
        country=run.locale.country,
        currency=run.locale.currency,
        frames=len(run.frames),
    )
    return assemble(
        sweep,
        room,
        lines,
        things,
        review,
        second_market=second_market(run),
        excluded_by_user=excluded,
        sweep_ended_at=ended,
        packet_at=datetime.now(UTC),
        stages=list(run.stages.values()),
        assumptions=assumptions(run),
        final=final,
    )


def assumptions(run: Run) -> list[str]:
    """What the packet assumes, stated from the settings it ran with."""
    s = settings()
    return [
        f"Prices are in {run.locale.currency} for {run.locale.country}; a price from another market is converted at the ECB rate of its day and marked converted.",
        "Replacement cost is a new copy of the same work from a retailer selling new books, the same format where the edition is known, else a current edition; used value is a second-hand listing in the condition it states.",
        f"Any line priced at or above {run.locale.appraisal_limit:,.0f} {run.locale.currency}, and anything the identifier judged rare, signed, antiquarian or first edition, goes to appraisal unpriced.",
        f"Identified lines with confidence under {s.review_confidence} are kept and also listed in the review queue.",
        "Spine sizes come from known-size objects located in the same frame (an A4 sheet, catalogue heights of identified books); a book's own catalogue height is left out of its measurement.",
        "Frames the frame model judged blurred or dark were not read.",
        "Item width and height come from the same frame scale, only for objects the reader placed at the depth of the spines; depth is not measured.",
        f"Room sizes come from an A4 sheet lying on the floor: floor points through its homography, heights through the camera pose it gives; wall area is perimeter times ceiling height less openings of at least {s.scoring.opening_min_m2} m2.",
        "The live session is billed from the tokens the Live API reports per turn, at list prices per modality; other stages from each call's tokens and searches.",
    ]


def crops(run: Run, packet: Packet) -> None:
    """Save each line's best sighting as a small picture beside the packet; a frame that cannot be opened leaves the line without one."""
    folder = run.run / "crops"
    folder.mkdir(exist_ok=True)
    frames = {r.frame.file: r.frame for r in run.readings.values()}
    for line in [*packet.books, *packet.items]:
        if not line.box_2d or line.frame_ref not in frames:
            continue
        try:
            (folder / f"{line.id}.jpg").write_bytes(crop(run.run, frames[line.frame_ref], line.box_2d))
        except OSError:
            continue
        line.crop_ref = f"crops/{line.id}.jpg"


def write(run: Run, final: bool) -> Packet:
    """Build the packet and write it and its report next to the frames; a deadline packet also keeps a copy of its own."""
    packet = build(run, run.room, final)
    crops(run, packet)
    for name in ["claim_packet.json"] + ["claim_packet_at_deadline.json"] * (not final):
        (run.run / name).write_text(packet.model_dump_json(indent=1), encoding="utf-8")
    (run.run / "report.html").write_text(render(packet), encoding="utf-8")
    return packet


async def close(run: Run) -> AsyncIterator[Packet]:
    """Finish a sweep: a packet from the work done when it falls due, then the final one when all work is done."""
    work = asyncio.ensure_future(run.finish())
    ended = run.ended or datetime.now(UTC)
    due = settings().packet_due_s - (datetime.now(UTC) - ended).total_seconds()
    done, _ = await asyncio.wait({work}, timeout=max(0.0, due))
    if not done:
        yield write(run, final=False)
    await work
    yield write(run, final=True)
