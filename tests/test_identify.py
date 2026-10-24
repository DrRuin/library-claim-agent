"""A book is confirmed by any page its search grounded on when the page the model named falls short."""

import asyncio

import pytest

from app import identify
from app.identify import Confirmed, Identity
from app.merge import Group

BOOK = Group(id="B1", unit="U1", shelf="S1", position=1, sightings=[], best="", notes=[], excluded_by="")


def named(title: str) -> Identity:
    """An identification of one title with its named page."""
    return Identity(
        verdict="identified",
        title=title,
        author="Mark Twain",
        publisher="",
        edition="",
        isbn="",
        confidence=0.9,
        evidence="spine",
        catalogue_url="https://named",
        appraisal_reason="",
    )


def stub(monkeypatch: pytest.MonkeyPatch, confirms: dict[str, bool]) -> None:
    """Pages that open and confirm or deny as given."""

    async def check(url: str, identity: Identity) -> tuple[str, Confirmed]:
        """Answer from the table."""
        return url, Confirmed(opened=True, confirms=confirms[url], height_mm=200, thickness_mm=20, reason=url)

    monkeypatch.setattr(identify, "check", check)


def test_a_grounded_page_confirms_when_the_named_one_does_not(monkeypatch: pytest.MonkeyPatch) -> None:
    """The named page is a listing without the title; the second grounded page shows it and becomes the source."""
    stub(monkeypatch, {"https://named": False, "https://a": False, "https://b": True})
    found = asyncio.run(identify.confirmed(BOOK, named("Life on the Mississippi"), ["https://a", "https://b"]))
    assert (found.identity.verdict, found.identity.catalogue_url, found.flags) == ("identified", "https://b", [])


def test_no_page_confirming_leaves_the_book_unidentified(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every page opened and none shows the work, so the title is cleared."""
    stub(monkeypatch, {"https://named": False, "https://a": False})
    found = asyncio.run(identify.confirmed(BOOK, named("Life on the Mississippi"), ["https://a"]))
    assert (found.identity.verdict, found.identity.title) == ("unidentified", "")
