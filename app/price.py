"""Prices the search found, each re-read on its page by a second model; code converts currencies."""

import asyncio
import json
from datetime import UTC, date, datetime
from functools import cache
from typing import Literal
from urllib.parse import urlparse

from pydantic import BaseModel

from app.config import Locale, settings
from app.llm import Page, fetch, opened, reread, search, web

PRICING = """- new: what buying this work new costs today: a new copy, the same format where the edition is known, sold by a bookshop, online bookstore or publisher selling to {country}. A marketplace reseller's listing marked new, a collectible copy or an import premium is not a replacement price; prefer the retailer's own price. When that format is out of print, price a new copy of a current edition of the same work. Only if no retailer in {country} sells it new, use a retailer in another country and give that currency.
- used: the price of a second-hand copy, from a used-book seller or marketplace listing in {country}, else in another country with its currency.
- Use only prices printed on a page this search found. quote copies the price text exactly as the page prints it. url is the address of the search result you read the price on, exactly as the search gave it; never a homepage or an address you composed.
- condition: for used, the condition the listing states, in its own words; for new, "new" and the format and edition of the copy priced.
- An offer you cannot find stays found=false with every other field empty. Never give a price from memory or from a typical price."""

BOOK = """<task>You price one book for an insurance contents claim in {country}, in {currency}. Search the web.</task>
<rules>
{pricing}
</rules>"""

ITEM = """<task>You price one household object for an insurance contents claim in {country}, in {currency}: the cost to replace it new today. Search the web.</task>
<rules>
- With a legible brand and model, find that model new at a retailer selling to {country}: status priced, low and high both that price, or the spread across the retailers you found.
- Without a brand and model, find at least two new comparable products of the same kind, size and material at retailers selling to {country}: status range, low the cheapest and high the dearest.
- art: paintings, prints, posters, photographs and portraits are needs_appraisal and get no price, unless a policyholder remark says it is a print or a poster, in which case price a comparable print.
- Use only prices printed on pages this search found. Each offer quotes the price text exactly as its page prints it, with its currency and url: the address of the search result you read it on, exactly as the search gave it; never a homepage or an address you composed.
- If you cannot find a price, status range with no offers.
</rules>"""

CHECK = """<task>Check one price on this page for an insurance claim. The page text follows when it could be fetched; otherwise open the page.</task>
<page>{url}</page>
<claim>{what} offered at {amount} {currency}; the page was said to print: {quote}</claim>
<rules>
- listed: the page shows this item, or this work in any format, with a price, whether or not it is in stock. A search page or a different item is not listed.
- price: the price the page itself prints for it, as a number in currency, whatever price the claim gives; empty when the page shows none.
- available: the page offers it for sale now (not sold out, out of stock or unavailable).
- same_format: the page's copy is the claim's format and edition, or the claim names none.
- opened: whether you could read the page at all.
- currency: the ISO 4217 code of the price the page prints.
- quote: that price exactly as the page prints it.
</rules>"""


class Offer(BaseModel):
    """A price as a page prints it."""

    found: bool
    amount: float | None
    currency: str
    seller: str
    url: str
    quote: str
    condition: str


class BookOffers(BaseModel):
    """New and used offers for one book."""

    new: Offer
    used: Offer


class ItemOffers(BaseModel):
    """Offers that bound one object's replacement cost."""

    status: Literal["priced", "range", "needs_appraisal"]
    offers: list[Offer]
    basis: str


class Check(BaseModel):
    """What the checker read on the page itself."""

    opened: bool
    listed: bool
    price: float | None
    currency: str
    quote: str = ""
    available: bool = True
    same_format: bool = True
    reason: str


class Priced(BaseModel):
    """An offer that survived the checks, in the claim currency."""

    amount: float
    source: str
    url: str
    retrieved_at: datetime
    converted: bool
    original_amount: float
    original_currency: str
    quote: str
    condition: str
    flags: list[str]
    rate: str = ""


@cache
def fx_cache() -> dict[tuple[str, str], tuple[float, date]]:
    """Exchange rates read in this process."""
    return {}


async def rate(base: str, quote: str) -> tuple[float, date]:
    """The ECB reference rate from `base` to `quote`, with its date."""
    if base == quote:
        return 1.0, datetime.now(UTC).date()
    if (base, quote) not in fx_cache():
        reply = await web().get(f"{settings().fx_url}/latest", params={"base": base, "symbols": quote})
        reply.raise_for_status()
        body = reply.json()
        fx_cache()[(base, quote)] = (body["rates"][quote], date.fromisoformat(body["date"]))
    return fx_cache()[(base, quote)]


async def check(offer: Offer, what: str, page: Page) -> Check:
    """A second model reads the price on the page itself."""
    prompt = CHECK.format(url=page.url, what=what, amount=offer.amount, currency=offer.currency, quote=offer.quote)
    return await reread(Check, prompt, page)


async def verify(offer: Offer, what: str, known: set[str], locale: Locale) -> tuple[Priced | None, str]:
    """Keep an offer only if the checker reads its price on the page, or cannot open a page the search was grounded on."""
    if not offer.found or offer.amount is None or not offer.url:
        return None, "no price found"
    page = await fetch(offer.url)
    url = page.url
    try:
        seen = await check(offer, what, page)
    except TimeoutError:
        seen = Check(opened=False, listed=False, price=None, currency="", reason="the check timed out")
    flags = []
    amount, currency, quote = offer.amount, offer.currency, offer.quote
    if seen.opened and (not seen.listed or seen.price is None):
        return None, f"the checker did not find it priced on the page: {seen.reason}"
    if seen.opened:
        amount, currency, quote = seen.price, seen.currency or currency, seen.quote or quote
        flags += ["the page shows it sold out or unavailable"] * (not seen.available)
        flags += [f"the page's copy is another format or edition: {seen.reason}"] * (not seen.same_format)
    elif {offer.url, url} & known:
        flags.append(
            "the checker could not reopen the page; the price is the search's reading of a page it was grounded on"
        )
    else:
        return None, f"neither the search nor the checker could open {url}"
    fx, day = await rate(currency, locale.currency)
    priced = Priced(
        amount=round(amount * fx, 2),
        source=offer.seller or urlparse(url).netloc,
        url=url,
        retrieved_at=datetime.now(UTC),
        converted=currency != locale.currency,
        original_amount=amount,
        original_currency=currency,
        quote=quote,
        condition=offer.condition,
        flags=flags,
        rate=f"converted from {currency} at the ECB rate of {day}" if currency != locale.currency else "",
    )
    return priced, ""


async def checked(
    identity: dict, offers: BookOffers, grounded: list[str], locale: Locale
) -> tuple[Priced | None, Priced | None, dict[str, str]]:
    """Both offers for one book checked on their pages, with the reason for each gap."""
    what = f"{identity['title']} by {identity['author']}" + (f", {identity['edition']}" if identity["edition"] else "")
    known = await opened(grounded)
    (new, why_new), (used, why_used) = await asyncio.gather(
        verify(offers.new, what, known, locale), verify(offers.used, f"a used copy of {what}", known, locale)
    )
    return new, used, {"replacement": why_new, "used": why_used}


async def book(identity: dict, locale: Locale) -> tuple[Priced | None, Priced | None, dict[str, str]]:
    """New and used prices of one identified book in one market, from a search of its own."""
    pricing = PRICING.format(country=locale.country)
    prompt = BOOK.format(country=locale.country, currency=locale.currency, pricing=pricing)
    offers, pages = await search(BookOffers, [prompt, json.dumps(identity, ensure_ascii=False)], settings().price_model)
    return await checked(identity, offers, [p.url for p in pages], locale)


async def item(thing: dict, remarks: list[dict], locale: Locale) -> tuple[str, list[Priced], str, list[str]]:
    """Status, checked offers, basis and gaps for one object."""
    prompt = ITEM.format(country=locale.country, currency=locale.currency)
    data = json.dumps({"object": thing, "policyholder_remarks": remarks}, ensure_ascii=False)
    offers, pages = await search(ItemOffers, [prompt, data], settings().price_model)
    known = await opened([p.url for p in pages])
    if offers.status == "needs_appraisal":
        return "needs_appraisal", [], offers.basis, []
    checked = await asyncio.gather(*(verify(o, thing["description"], known, locale) for o in offers.offers))
    kept = [p for p, _ in checked if p]
    reasons = [why for p, why in checked if not p]
    return offers.status, kept, offers.basis, reasons
